"""Pluggable web search providers for website discovery.

Each provider returns a list of {"url", "title", "snippet"} dicts or raises
SearchError. Add a new provider by subclassing SearchProvider and registering
it in PROVIDERS. API keys come from .env (see config.py) and are never logged.

Supported:
  brave       Brave Search API (paid, prepaid credits; Brave includes a monthly free credit)
  google_cse  Google Programmable Search JSON API. Closed to new customers;
              existing keys stop working on 2027-01-01.
  searxng     A SearXNG instance you run yourself (free; JSON output must be enabled)
  manual      DEFAULT, free: no automatic search. You run generated queries in your
              browser and import the result URLs (see free_discovery.py)
  none        Same as manual (kept for older .env files)

A paid provider without a key falls back to `manual`; nothing switches to a paid service.
"""
import httpx

from . import config


class SearchError(Exception):
    def __init__(self, message: str, quota: bool = False):
        super().__init__(config.redact(message))
        self.quota = quota  # True when the provider says a limit/quota was hit: stop searching this run


class SearchProvider:
    name = "base"
    automatic = True  # False: no automatic searching (free manual import workflow)

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=config.HTTP_TIMEOUT_SECONDS, follow_redirects=True)

    def config_problem(self) -> str:
        return ""

    def search(self, query: str, count: int = 20) -> list[dict]:
        raise NotImplementedError

    def _get(self, url: str, **kwargs) -> dict:
        try:
            r = self.client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise SearchError(f"{self.name}: network error ({type(exc).__name__})") from None
        if r.status_code == 429:
            raise SearchError(f"{self.name}: rate limit or quota reached (HTTP 429)", quota=True)
        if r.status_code in (401, 403):
            raise SearchError(f"{self.name}: API key rejected or plan does not allow this request (HTTP {r.status_code})", quota=True)
        if r.status_code >= 400:
            raise SearchError(f"{self.name}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise SearchError(f"{self.name}: response was not JSON") from None


class BraveSearch(SearchProvider):
    name = "brave"
    URL = "https://api.search.brave.com/res/v1/web/search"

    def config_problem(self) -> str:
        return "" if config.BRAVE_API_KEY else "BRAVE_API_KEY is not set in .env."

    def search(self, query: str, count: int = 20) -> list[dict]:
        data = self._get(self.URL, params={"q": query, "count": min(max(count, 1), 20)},
                         headers={"Accept": "application/json", "X-Subscription-Token": config.BRAVE_API_KEY})
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("description", "")}
                for r in (data.get("web") or {}).get("results", []) if r.get("url")]


class GoogleCSESearch(SearchProvider):
    name = "google_cse"
    URL = "https://www.googleapis.com/customsearch/v1"

    def config_problem(self) -> str:
        if not (config.GOOGLE_CSE_API_KEY and config.GOOGLE_CSE_ID):
            return "GOOGLE_CSE_API_KEY and GOOGLE_CSE_ID must both be set in .env."
        return ""

    def search(self, query: str, count: int = 20) -> list[dict]:
        out = []
        for start in range(1, min(count, 20) + 1, 10):  # 10 results per call
            data = self._get(self.URL, params={"key": config.GOOGLE_CSE_API_KEY, "cx": config.GOOGLE_CSE_ID, "q": query,
                                               "num": 10, "start": start})
            items = data.get("items", [])
            out += [{"url": i.get("link", ""), "title": i.get("title", ""), "snippet": i.get("snippet", "")} for i in items if i.get("link")]
            if len(items) < 10:
                break
        return out[:count]


class SearxngSearch(SearchProvider):
    name = "searxng"

    def config_problem(self) -> str:
        return "" if config.SEARXNG_URL else "SEARXNG_URL is not set in .env (e.g. http://127.0.0.1:8080)."

    def search(self, query: str, count: int = 20) -> list[dict]:
        data = self._get(config.SEARXNG_URL.rstrip("/") + "/search", params={"q": query, "format": "json"})
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("content", "")}
                for r in data.get("results", []) if r.get("url")][:count]


class ManualImport(SearchProvider):
    """The free default: no automatic searching. You run the generated queries in your
    browser and import the result URLs (see free_discovery.py)."""
    name = "manual"
    automatic = False

    def search(self, query: str, count: int = 20) -> list[dict]:
        return []


class NoSearch(ManualImport):
    name = "none"


PROVIDERS = {p.name: p for p in (BraveSearch, GoogleCSESearch, SearxngSearch, ManualImport, NoSearch)}
PAID = {"brave": "Brave Search API is a paid, per-request service",
        "google_cse": "Google Programmable Search is closed to new customers and paid beyond its daily free quota"}
_override: SearchProvider | None = None


def set_provider_override(p: SearchProvider | None) -> None:
    """Tests inject a fake provider here."""
    global _override
    _override = p


def resolve_provider() -> tuple[SearchProvider, dict]:
    """The provider discovery will actually use, and a plain explanation for the dashboard.
    A provider that isn't configured falls back to the FREE manual workflow. Nothing ever
    falls back to (or silently switches to) a paid service."""
    if _override is not None:
        auto = getattr(_override, "automatic", True)
        return _override, {"configured": _override.name, "active": _override.name, "automatic": auto,
                           "paid": False, "reason": ""}
    configured = config.SEARCH_PROVIDER
    manual_reason = ("Run the generated searches in your own browser and import the result URLs; the app "
                     "then inspects each website automatically.")
    cls = PROVIDERS.get(configured)
    if cls is None:
        return ManualImport(), {"configured": configured, "active": "manual", "automatic": False, "paid": False,
                                "reason": f"Unknown SEARCH_PROVIDER '{configured}', so automatic searching is off. {manual_reason}"}
    p = cls()
    if not getattr(p, "automatic", True):
        return p, {"configured": configured, "active": "manual", "automatic": False, "paid": False,
                   "reason": f"SEARCH_PROVIDER={configured}: automatic searching is off (free mode). {manual_reason}"}
    problem = p.config_problem()
    if problem:
        why = f" ({PAID[configured]})" if configured in PAID else ""
        return ManualImport(), {"configured": configured, "active": "manual", "automatic": False, "paid": False,
                                "reason": f"Automatic search is unavailable: SEARCH_PROVIDER={configured} but {problem}{why} "
                                          f"Using the free manual workflow instead. {manual_reason} "
                                          f"Set SEARCH_PROVIDER=manual in .env to make this the explicit choice."}
    return p, {"configured": configured, "active": configured, "automatic": True, "paid": configured in PAID,
               "reason": (f"Automatic search uses {configured}. {PAID[configured]}; you configured its key yourself."
                          if configured in PAID else f"Automatic search uses your own {configured} instance (free).")}


def get_provider() -> SearchProvider:
    return resolve_provider()[0]
