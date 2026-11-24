"""Configuration loaded from environment variables (.env file).

Secrets (SMTP password, OAuth client secret) are only ever read from the
environment or from files listed in .gitignore. They are never logged.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Hard safety ceiling: no batch may ever contain more than this many emails,
# regardless of what settings say.
MAX_BATCH_SIZE = 30
# One follow-up per original email, never more.
MAX_FOLLOWUPS_PER_EMAIL = 1


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
DATABASE_PATH = Path(_env("DATABASE_PATH", str(DATA_DIR / "outreach.db")))

# Gmail OAuth files (both are git-ignored).
GMAIL_CLIENT_SECRET_FILE = Path(_env("GMAIL_CLIENT_SECRET_FILE", str(BASE_DIR / "credentials" / "client_secret.json")))
GMAIL_TOKEN_FILE = Path(_env("GMAIL_TOKEN_FILE", str(BASE_DIR / "credentials" / "gmail_token.json")))
# gmail.send to send; gmail.readonly to detect replies/bounces and show a reply's text when you click
# "View reply" (the app only opens messages it identified as replies to your outreach; read-only can't
# delete, change or send anything). gmail.metadata is NOT requested together with readonly: Google then
# refuses full-message reads. Older tokens (send-only, or send+metadata) keep working for sending.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE]
REPLY_CHECK_AUTO = _env("REPLY_CHECK_AUTO", "true").lower() in ("1", "true", "yes", "on")
REPLY_CHECK_MINUTES = max(5, int(_env("REPLY_CHECK_MINUTES", "15") or 15))
REPLY_CHECK_DAYS = int(_env("REPLY_CHECK_DAYS", "60") or 60)  # watch sent emails this many days back

# SMTP alternative.
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = _env_int("SMTP_PORT", 587)
SMTP_USERNAME = _env("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")  # never log this
SMTP_USE_SSL = _env("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes")
SMTP_TIMEOUT = _env_int("SMTP_TIMEOUT", 30)

# Defaults for settings that can later be changed in the dashboard.
_SENDER_NAME = _env("SENDER_NAME")
DEFAULTS = {
    "provider": _env("EMAIL_PROVIDER", "dryrun"),  # dryrun | gmail | smtp
    "test_mode": _env("TEST_MODE", "true"),
    "test_recipient": _env("TEST_RECIPIENT"),
    "sender_name": _SENDER_NAME,
    "sender_email": _env("SENDER_EMAIL"),
    "sender_description": _env(
        "SENDER_DESCRIPTION",
        f"I am {_SENDER_NAME + ', a' if _SENDER_NAME else 'a'} content writer and researcher"
        " specializing in education, technology, and business services.",
    ),
    "sender_website": _env("SENDER_WEBSITE"),
    "sender_postal_address": _env("SENDER_POSTAL_ADDRESS"),
    "send_delay_seconds": _env("SEND_DELAY_SECONDS", "45"),
    "daily_send_limit": _env("DAILY_SEND_LIMIT", "50"),
    "followups_enabled": "false",
    "followup_delay_days": "7",
}

# ---- Website discovery -------------------------------------------------
# API keys are read from the environment only and never logged, stored in the
# database, or written to CSV. Leave a key empty to disable that provider.

def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


OUTPUT_DIR = Path(_env("OUTPUT_DIR", str(BASE_DIR / "output")))
OUTREACH_CSV_PATH = OUTPUT_DIR / "niche_websites_outreach.csv"

# Search provider: manual (free default) | searxng (free, self-hosted) | brave | google_cse | none
SEARCH_PROVIDER = _env("SEARCH_PROVIDER", "manual").lower()
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "").strip()
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "").strip()
GOOGLE_CSE_ID = _env("GOOGLE_CSE_ID")
SEARXNG_URL = _env("SEARXNG_URL")

# SEO metrics. Moz DA via the Moz API (JSON-RPC); Ahrefs DR via the free
# Domain Rating endpoint (needs a free Ahrefs API v3 key) or the paid endpoint.
MOZ_API_TOKEN = os.getenv("MOZ_API_TOKEN", "").strip()
AHREFS_API_KEY = os.getenv("AHREFS_API_KEY", "").strip()
AHREFS_DR_ENDPOINT = _env("AHREFS_DR_ENDPOINT", "free").lower()  # free | paid

# Email verification: none | hunter | zerobounce
EMAIL_VERIFY_PROVIDER = _env("EMAIL_VERIFY_PROVIDER", "none").lower()
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "").strip()
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY", "").strip()

# Limits and budgets.
DISCOVERY_AUTO_ON_STARTUP = _env_bool("DISCOVERY_AUTO_ON_STARTUP", True)
DISCOVERY_DAILY_LIMIT = _env_int("DISCOVERY_DAILY_LIMIT", 25)            # new domains crawled per run
DISCOVERY_IMPORT_MAX_PER_RUN = _env_int("DISCOVERY_IMPORT_MAX_PER_RUN", 50)  # imported websites inspected per run
DISCOVERY_MAX_SEARCHES = _env_int("DISCOVERY_MAX_SEARCHES", 12)          # search API calls per run
DISCOVERY_MAX_SEARCHES_MONTH = _env_int("DISCOVERY_MAX_SEARCHES_MONTH", 900)
DISCOVERY_RESULTS_PER_QUERY = _env_int("DISCOVERY_RESULTS_PER_QUERY", 20)
DISCOVERY_MAX_REQUESTS_PER_DOMAIN = _env_int("DISCOVERY_MAX_REQUESTS_PER_DOMAIN", 6)
DISCOVERY_REQUEST_DELAY_SECONDS = float(_env("DISCOVERY_REQUEST_DELAY_SECONDS", "1.5") or 1.5)
DISCOVERY_SEARCH_CACHE_DAYS = _env_int("DISCOVERY_SEARCH_CACHE_DAYS", 14)
DISCOVERY_REVISIT_DAYS = _env_int("DISCOVERY_REVISIT_DAYS", 30)
METRICS_MAX_LOOKUPS_PER_RUN = _env_int("METRICS_MAX_LOOKUPS_PER_RUN", 25)
METRICS_REFRESH_DAYS = _env_int("METRICS_REFRESH_DAYS", 30)
EMAIL_VERIFY_MAX_PER_RUN = _env_int("EMAIL_VERIFY_MAX_PER_RUN", 25)
EMAIL_VERIFY_CACHE_DAYS = _env_int("EMAIL_VERIFY_CACHE_DAYS", 30)
HTTP_TIMEOUT_SECONDS = _env_int("HTTP_TIMEOUT_SECONDS", 15)

# Unattended discovery by Claude Code (uses your Claude plan; no API key, no paid service).
CLAUDE_CLI_PATH = _env("CLAUDE_CLI_PATH")                                  # blank = find `claude` automatically
CLAUDE_DISCOVERY_PER_NICHE = _env_int("CLAUDE_DISCOVERY_PER_NICHE", 8)     # target new sites per niche per run
CLAUDE_DISCOVERY_MAX_TURNS = _env_int("CLAUDE_DISCOVERY_MAX_TURNS", 40)
CLAUDE_DISCOVERY_TIMEOUT_SECONDS = _env_int("CLAUDE_DISCOVERY_TIMEOUT_SECONDS", 1500)
CRAWLER_USER_AGENT = _env(
    "CRAWLER_USER_AGENT",
    "OutreachResearchBot/1.0 (+contributor research; respects robots.txt)",
)


# ---- Hosting (Render etc.) ---------------------------------------------
# APP_PASSWORD turns on the login page. It is REQUIRED when the app runs on a server:
# without it, anyone who finds the address could send email from your Gmail.
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")          # random string; signs the login cookie
SESSION_DAYS = _env_int("SESSION_DAYS", 14)
# Public address of the hosted app, e.g. https://outreach-xyz.onrender.com . When set, Gmail is
# connected with the web sign-in flow (redirect to PUBLIC_BASE_URL/oauth2callback).
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
# The Google "Web application" OAuth client JSON, pasted into an environment variable (hosting has no file upload).
GMAIL_CLIENT_SECRET_JSON = os.getenv("GMAIL_CLIENT_SECRET_JSON", "")
ON_SERVER = bool(os.getenv("RENDER") or PUBLIC_BASE_URL)


def secret_values() -> list[str]:
    """Every configured secret, so error messages can be scrubbed before storage."""
    return [s for s in (BRAVE_API_KEY, GOOGLE_CSE_API_KEY, MOZ_API_TOKEN, AHREFS_API_KEY, HUNTER_API_KEY,
                        ZEROBOUNCE_API_KEY, SMTP_PASSWORD, APP_PASSWORD, SESSION_SECRET) if s and len(s) >= 4]


def redact(text: str) -> str:
    text = str(text or "")
    for s in secret_values():
        text = text.replace(s, "***")
    return text


# Placeholder addresses shipped in .env.example. They must never end up in an email.
PLACEHOLDER_EMAILS = ("your.address@example.com", "your.own.address@example.com")


def sender_email_problem(email: str) -> str:
    """Why this sender email can't be used, or "" if it's fine."""
    email = (email or "").strip().lower()
    if not email:
        return "Sender email is not set. Add SENDER_EMAIL to .env or set it in Settings."
    if email in PLACEHOLDER_EMAILS:
        return f"Sender email is still the placeholder {email}. Set SENDER_EMAIL in .env or in Settings."
    return ""
