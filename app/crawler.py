"""Polite page fetcher used by website discovery.

* Checks robots.txt for our user agent before every fetch (a missing robots.txt
  allows crawling; an unreadable one, e.g. HTTP 401/403, is treated as "disallow").
* At most `max_requests_per_domain` requests per root domain per run, robots.txt included.
* Waits `delay` seconds between requests to the same host.
* Only HTML is read, and only the first MAX_BYTES of it.
* Never tries to get past CAPTCHAs, logins, paywalls or bot challenges: such
  pages raise Blocked and the website is left for manual review.
"""
import time
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from . import config
from .domains import root_domain

MAX_BYTES = 1_500_000
CHALLENGE_MARKERS = ("cf-challenge", "cf_chl_", "challenge-platform", "g-recaptcha", "h-captcha", "hcaptcha.com",
                     "captcha-delivery", "are you a robot", "verify you are human", "checking your browser",
                     "attention required! | cloudflare")


class FetchError(Exception):
    pass


class Blocked(FetchError):
    """The site refused automated access (robots.txt, bot challenge, 401/403/429)."""


class Page:
    def __init__(self, url: str, final_url: str, status: int, html: str):
        self.url, self.final_url, self.status, self.html = url, final_url, status, html


class _Raw:
    def __init__(self, status_code: int, url: str, content: bytes, encoding: str):
        self.status_code, self.url, self.content, self.encoding = status_code, url, content, encoding


class Crawler:
    def __init__(self, client: httpx.Client | None = None, max_requests_per_domain: int | None = None,
                 delay: float | None = None, sleep=time.sleep):
        self.client = client or httpx.Client(
            timeout=config.HTTP_TIMEOUT_SECONDS, follow_redirects=True,
            headers={"User-Agent": config.CRAWLER_USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        self.max_requests = max_requests_per_domain or config.DISCOVERY_MAX_REQUESTS_PER_DOMAIN
        self.delay = config.DISCOVERY_REQUEST_DELAY_SECONDS if delay is None else delay
        self.sleep = sleep
        self.requests_by_domain: dict[str, int] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self.total_requests = 0

    def budget_left(self, url: str) -> int:
        return self.max_requests - self.requests_by_domain.get(root_domain(url), 0)

    def _request(self, url: str) -> "_Raw":
        domain = root_domain(url)
        if self.budget_left(url) <= 0:
            raise FetchError(f"Per-domain request limit ({self.max_requests}) reached for {domain}")
        host = urlparse(url).hostname or ""
        wait = self.delay - (time.monotonic() - self._last_hit.get(host, -1e9))
        if wait > 0:
            self.sleep(wait)
        self.requests_by_domain[domain] = self.requests_by_domain.get(domain, 0) + 1
        self.total_requests += 1
        try:
            with self.client.stream("GET", url) as r:
                chunks, size = [], 0
                ctype = r.headers.get("content-type", "")
                if r.status_code < 400 and ("html" in ctype or "text/plain" in ctype or not ctype):
                    for chunk in r.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BYTES:
                            break
                return _Raw(r.status_code, str(r.url), b"".join(chunks), r.encoding or "utf-8")
        except httpx.HTTPError as exc:
            raise FetchError(f"Could not fetch {url} ({type(exc).__name__})") from None
        finally:
            self._last_hit[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self._request(base + "/robots.txt")
            except FetchError:
                self._robots[base] = None  # unreachable robots.txt: be conservative
                return False
            if r.status_code in (401, 403):
                self._robots[base] = None
            elif r.status_code >= 400:
                rp.parse([])  # no robots.txt: everything allowed
                self._robots[base] = rp
            else:
                rp.parse(r.content.decode("utf-8", "replace").splitlines())
                self._robots[base] = rp
        rp = self._robots[base]
        return rp is not None and rp.can_fetch(config.CRAWLER_USER_AGENT, url)

    def fetch(self, url: str) -> Page:
        if not self.allowed(url):
            raise Blocked(f"robots.txt does not allow fetching {url}")
        r = self._request(url)
        if r.status_code in (401, 403, 429, 503):
            raise Blocked(f"{url} refused automated access (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise FetchError(f"{url} returned HTTP {r.status_code}")
        try:
            html = r.content.decode(r.encoding, "replace")
        except LookupError:
            html = r.content.decode("utf-8", "replace")
        head = html[:20000].lower()
        if any(m in head for m in CHALLENGE_MARKERS) and len(html) < 60000:
            raise Blocked(f"{url} shows a bot check / CAPTCHA; not bypassed")
        return Page(url, r.url, r.status_code, html)
