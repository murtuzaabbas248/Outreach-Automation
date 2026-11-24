"""Email verification statuses for discovered contacts.

  Verified      a configured verification service reported the mailbox as valid
  Not Checked   well formed, but no verification service checked it (default without a service)
  Syntax Valid  same meaning as Not Checked; used by older records, which are left unchanged
  Catch-All     the domain accepts every address, so this mailbox is unconfirmed
  Invalid       the verification service reported the address as invalid / unsafe to mail
  Unknown       no reliable result

Syntax alone never produces "Verified". MX/SMTP probing is not done here: it
cannot prove a specific mailbox exists and is often treated as abusive.
"""
import httpx

from . import config, db
from .validation import is_valid_email, normalize_email

VERIFIED, SYNTAX_VALID, CATCH_ALL, INVALID, UNKNOWN = "Verified", "Syntax Valid", "Catch-All", "Invalid", "Unknown"
NOT_CHECKED = "Not Checked"  # well-formed, but no verification service checked it (the free default)
STATUSES = (VERIFIED, NOT_CHECKED, SYNTAX_VALID, CATCH_ALL, INVALID, UNKNOWN)  # Syntax Valid: older records only

HUNTER_MAP = {"valid": VERIFIED, "invalid": INVALID, "accept_all": CATCH_ALL, "disposable": INVALID,
              "webmail": UNKNOWN, "unknown": UNKNOWN}
ZEROBOUNCE_MAP = {"valid": VERIFIED, "invalid": INVALID, "catch-all": CATCH_ALL, "unknown": UNKNOWN,
                  "spamtrap": INVALID, "abuse": INVALID, "do_not_mail": INVALID}


class VerifyError(Exception):
    def __init__(self, message: str, quota: bool = False):
        super().__init__(config.redact(message))
        self.quota = quota


class HunterVerifier:
    name = "hunter"
    URL = "https://api.hunter.io/v2/email-verifier"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=30)

    def verify(self, email: str) -> tuple[str, str]:
        for _ in range(2):  # HTTP 202 = still running; one retry, polling counts as the same request
            try:
                r = self.client.get(self.URL, params={"email": email, "api_key": config.HUNTER_API_KEY})
            except httpx.HTTPError as exc:
                raise VerifyError(f"Hunter: network error ({type(exc).__name__})") from None
            if r.status_code != 202:
                break
        if r.status_code == 202:
            return UNKNOWN, "pending"
        if r.status_code in (401, 403, 429):
            raise VerifyError(f"Hunter: HTTP {r.status_code} (key, plan or rate limit)", quota=True)
        if r.status_code >= 400:
            raise VerifyError(f"Hunter: HTTP {r.status_code}")
        raw = str(((r.json() or {}).get("data") or {}).get("status", "unknown")).lower()
        return HUNTER_MAP.get(raw, UNKNOWN), raw


class ZeroBounceVerifier:
    name = "zerobounce"
    URL = "https://api.zerobounce.net/v2/validate"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=30)

    def verify(self, email: str) -> tuple[str, str]:
        try:
            r = self.client.get(self.URL, params={"api_key": config.ZEROBOUNCE_API_KEY, "email": email, "ip_address": ""})
        except httpx.HTTPError as exc:
            raise VerifyError(f"ZeroBounce: network error ({type(exc).__name__})") from None
        if r.status_code in (401, 403, 429):
            raise VerifyError(f"ZeroBounce: HTTP {r.status_code}", quota=True)
        if r.status_code >= 400:
            raise VerifyError(f"ZeroBounce: HTTP {r.status_code}")
        data = r.json() or {}
        if data.get("error"):
            raise VerifyError(f"ZeroBounce: {data['error']}", quota="credit" in str(data["error"]).lower())
        raw = str(data.get("status", "unknown")).lower()
        return ZEROBOUNCE_MAP.get(raw, UNKNOWN), raw


_override = None


def set_override(v) -> None:
    global _override
    _override = v


def get_verifier():
    if _override is not None:
        return _override
    if config.EMAIL_VERIFY_PROVIDER == "hunter" and config.HUNTER_API_KEY:
        return HunterVerifier()
    if config.EMAIL_VERIFY_PROVIDER == "zerobounce" and config.ZEROBOUNCE_API_KEY:
        return ZeroBounceVerifier()
    return None


def config_problem() -> str:
    p = config.EMAIL_VERIFY_PROVIDER
    if p == "none":
        return "No email verification service configured, so email status stays Not Checked (never guessed)."
    if p == "hunter" and not config.HUNTER_API_KEY:
        return "EMAIL_VERIFY_PROVIDER=hunter but HUNTER_API_KEY is empty."
    if p == "zerobounce" and not config.ZEROBOUNCE_API_KEY:
        return "EMAIL_VERIFY_PROVIDER=zerobounce but ZEROBOUNCE_API_KEY is empty."
    if p not in ("hunter", "zerobounce"):
        return f"Unknown EMAIL_VERIFY_PROVIDER '{p}'."
    return ""


def local_status(email: str) -> tuple[str, str]:
    """Status without a verification service: Not Checked. A format check is not verification."""
    if not email or not is_valid_email(email):
        return UNKNOWN, ""
    return NOT_CHECKED, "format OK; mailbox not verified"


def cached(conn, email: str) -> tuple[str, str] | None:
    """A recent provider result for this address, or None."""
    c = db.row(conn, "SELECT * FROM email_checks WHERE email_norm=?", (normalize_email(email),))
    if c and c["checked_at"] >= _cutoff():
        return c["status"], f"{c['provider']} ({c['checked_at'][:10]})"
    return None


def store(conn, email: str, status: str, raw: str, provider: str) -> str:
    ts = db.now()
    conn.execute("INSERT INTO email_checks(email_norm, status, provider, raw_status, checked_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(email_norm) DO UPDATE SET status=excluded.status, provider=excluded.provider, "
                 "raw_status=excluded.raw_status, checked_at=excluded.checked_at",
                 (normalize_email(email), status, provider, raw, ts))
    return f"{provider} ({ts[:10]})"


def _cutoff() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=config.EMAIL_VERIFY_CACHE_DAYS)).replace(microsecond=0).isoformat()
