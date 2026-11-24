"""Domain Authority (Moz) and Domain Rating (Ahrefs) lookups.

* DA comes only from Moz; DR comes only from Ahrefs. They are different metrics
  from different companies, and neither is Google PageRank.
* A value is stored only when the provider returns a number from 0 to 100.
  Nothing is ever estimated. If a provider isn't configured, the value stays blank.

Moz:    POST https://api.moz.com/jsonrpc, header x-moz-token, method data.site.metrics.fetch
Ahrefs: POST https://api.ahrefs.com/v3/public/domain-rating-free (free; needs a free API v3 key), or
        GET  https://api.ahrefs.com/v3/site-explorer/domain-rating (paid plan; uses API units).
        Data is "Domain Rating by Ahrefs" under https://ahrefs.com/legal/domain-rating-license
"""
import uuid
from datetime import date

import httpx

from . import config

MOZ_URL = "https://api.moz.com/jsonrpc"
AHREFS_FREE_URL = "https://api.ahrefs.com/v3/public/domain-rating-free"
AHREFS_PAID_URL = "https://api.ahrefs.com/v3/site-explorer/domain-rating"
MOZ_SOURCE = "Moz API (data.site.metrics.fetch)"
AHREFS_FREE_SOURCE = "Domain Rating by Ahrefs (free endpoint)"
AHREFS_PAID_SOURCE = "Domain Rating by Ahrefs (API v3 site-explorer)"


class MetricError(Exception):
    def __init__(self, message: str, quota: bool = False):
        super().__init__(config.redact(message))
        self.quota = quota


def valid_metric(value) -> float | None:
    """Accept only a real number in 0..100; anything else is treated as 'no value'."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if 0 <= v <= 100 else None


def _client(client):
    return client or httpx.Client(timeout=config.HTTP_TIMEOUT_SECONDS)


def _check(r: httpx.Response, name: str) -> dict:
    if r.status_code == 429:
        raise MetricError(f"{name}: rate limit or quota reached (HTTP 429)", quota=True)
    if r.status_code in (401, 402, 403):
        raise MetricError(f"{name}: key rejected, or your plan does not include this request (HTTP {r.status_code})", quota=True)
    if r.status_code >= 400:
        raise MetricError(f"{name}: HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise MetricError(f"{name}: response was not JSON") from None


class MozDA:
    name = "moz"
    source = MOZ_SOURCE

    def __init__(self, client: httpx.Client | None = None):
        self.client = _client(client)

    @staticmethod
    def config_problem() -> str:
        return "" if config.MOZ_API_TOKEN else "MOZ_API_TOKEN is not set in .env, so DA is left blank."

    def fetch(self, domains: list[str]) -> dict[str, float | None]:
        out = {}
        for d in domains:
            body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "data.site.metrics.fetch",
                    "params": {"data": {"site_query": {"query": d, "scope": "domain"}}}}
            try:
                r = self.client.post(MOZ_URL, json=body, headers={"x-moz-token": config.MOZ_API_TOKEN,
                                                                 "Content-Type": "application/json"})
            except httpx.HTTPError as exc:
                raise MetricError(f"Moz: network error ({type(exc).__name__})") from None
            data = _check(r, "Moz")
            if data.get("error"):
                err = data["error"]
                msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                quota = any(w in msg.lower() for w in ("quota", "limit", "credit", "insufficient"))
                if quota:
                    raise MetricError(f"Moz: {msg}", quota=True)
                out[d] = None
                continue
            metrics = ((data.get("result") or {}).get("site_metrics") or {})
            out[d] = valid_metric(metrics.get("domain_authority"))
        return out


class AhrefsDR:
    name = "ahrefs"

    def __init__(self, client: httpx.Client | None = None):
        self.client = _client(client)
        self.paid = config.AHREFS_DR_ENDPOINT == "paid"
        self.source = AHREFS_PAID_SOURCE if self.paid else AHREFS_FREE_SOURCE

    @staticmethod
    def config_problem() -> str:
        return "" if config.AHREFS_API_KEY else "AHREFS_API_KEY is not set in .env, so DR is left blank."

    def _headers(self):
        return {"Authorization": f"Bearer {config.AHREFS_API_KEY}", "Accept": "application/json"}

    def fetch(self, domains: list[str]) -> dict[str, float | None]:
        if not domains:
            return {}
        try:
            if self.paid:
                out = {}
                for d in domains:
                    r = self.client.get(AHREFS_PAID_URL, params={"target": d, "date": date.today().isoformat()},
                                        headers=self._headers())
                    out[d] = valid_metric(((_check(r, "Ahrefs").get("domain_rating") or {}).get("domain_rating")))
                return out
            out = {d: None for d in domains}
            for i in range(0, len(domains), 1000):  # the free endpoint takes up to 1000 targets per request
                chunk = domains[i:i + 1000]
                r = self.client.post(AHREFS_FREE_URL, json={"targets": chunk}, headers=self._headers())
                data = _check(r, "Ahrefs")
                for t in ((data.get("domain_rating") or {}).get("targets") or []):
                    key = str(t.get("target", "")).lower().removeprefix("www.").rstrip("/")
                    if key in out:
                        out[key] = valid_metric(t.get("domain_rating"))
            return out
        except httpx.HTTPError as exc:
            raise MetricError(f"Ahrefs: network error ({type(exc).__name__})") from None


def import_values(values: dict, metric: str, source: str) -> dict:
    """Store DA or DR values that came from a real provider outside this app (e.g. Ahrefs'
    free Domain Rating lookup run through Claude's Ahrefs connector). Only numbers 0..100 for
    websites already in the database are stored; anything else is reported and left Not Checked."""
    from . import db
    from .domains import root_domain
    from .websites import log_change

    if metric not in ("da", "dr"):
        raise ValueError("metric must be 'da' or 'dr'")
    if not source.strip():
        raise ValueError("a source is required, e.g. 'Domain Rating by Ahrefs (free)'")
    report = {"updated": 0, "unchanged": 0, "not_in_database": [], "invalid": []}
    ts = db.now()
    with db.transaction() as conn:
        for raw, value in values.items():
            domain = root_domain(str(raw))
            v = valid_metric(value)
            if v is None:
                report["invalid"].append(str(raw))
                continue
            w = db.row(conn, "SELECT * FROM websites WHERE domain=?", (domain,))
            if not w:
                report["not_in_database"].append(domain)
                continue
            if w[metric] == v and w[f"{metric}_source"] == source:
                report["unchanged"] += 1
                continue
            conn.execute(f"UPDATE websites SET {metric}=?, {metric}_source=?, {metric}_checked_at=?, updated_at=? WHERE id=?",
                         (v, source, ts, ts, w["id"]))
            log_change(conn, w["id"], metric, w[metric], v, source)
            report["updated"] += 1
    return report


_overrides: dict[str, object] = {}


def set_override(name: str, provider) -> None:
    """Tests inject fake metric providers ('moz' / 'ahrefs'); None removes the override."""
    if provider is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = provider


def get_moz():
    if "moz" in _overrides:
        return _overrides["moz"]
    return MozDA() if not MozDA.config_problem() else None


def get_ahrefs():
    if "ahrefs" in _overrides:
        return _overrides["ahrefs"]
    return AhrefsDR() if not AhrefsDR.config_problem() else None
