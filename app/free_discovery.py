"""Zero-cost website discovery: search-query generator + URL import queue.

No paid API is involved:

1. The app generates niche-specific search queries and gives you links that
   open each one in a normal search engine in YOUR browser (Google, Bing,
   DuckDuckGo, Brave Search). You do the searching by hand, like any visitor.
   The app itself never queries or scrapes search engines (their terms forbid
   automated querying) and never touches CAPTCHAs, logins or rate limits.
2. You paste the result URLs (or copied result text, or a saved .html results
   page, or any list of links) into the dashboard.
3. URLs are normalized, deduplicated by root domain against the database and the
   queue, then inspected by the polite crawler (robots.txt, per-domain request
   cap, delay) which looks for guest post / contribution guidelines, contact
   pages, relevant articles and public email addresses.
"""
import base64
import re
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from . import db
from .domains import clean_url, root_domain
from .qualify import GUIDELINE_RE, NICHE_LABELS, blocked_domain

# ------------------------------------------------------------------ query generation

# Search operators work in Google and Bing; DuckDuckGo/Brave ignore the ones they don't support.
FOOTPRINTS = [
    '{kw} "write for us"',
    '{kw} "guest post guidelines"',
    '{kw} "submit a guest post"',
    '{kw} "become a contributor"',
    '{kw} "contributor guidelines"',
    '{kw} intitle:"write for us"',
    '{kw} inurl:write-for-us',
    '{kw} "accepting guest posts"',
]

SEARCH_ENGINES = {  # opened in your own browser; nothing is fetched by the app
    "Google": "https://www.google.com/search?q={q}",
    "Bing": "https://www.bing.com/search?q={q}",
    "DuckDuckGo": "https://duckduckgo.com/?q={q}",
    "Brave": "https://search.brave.com/search?q={q}",
}

MAX_KEYWORDS = 20


def search_links(query: str) -> dict[str, str]:
    return {name: tpl.format(q=quote_plus(query)) for name, tpl in SEARCH_ENGINES.items()}


def generate_queries(niche: str = "", keywords: str | list[str] = "") -> list[dict]:
    """Niche-specific queries. With keywords, every footprint is applied to each keyword;
    without, the curated query bank is used. `niche` is a niche key or '' for all."""
    from .discovery import QUERY_BANK  # local import: discovery imports this module

    if isinstance(keywords, str):
        keywords = re.split(r"[,\n;]+", keywords)
    kws = []
    for k in keywords:
        k = re.sub(r"\s+", " ", k).strip().strip('"')[:80]
        if k and k.lower() not in (x.lower() for x in kws):
            kws.append(k)
    kws = kws[:MAX_KEYWORDS]
    niches = [niche] if niche in QUERY_BANK else list(QUERY_BANK)
    out, seen = [], set()

    def add(n, q):
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append({"niche": n, "niche_label": NICHE_LABELS.get(n, ""), "query": q, "links": search_links(q)})

    if kws:
        for kw in kws:
            term = f'"{kw}"' if " " in kw else kw
            for fp in FOOTPRINTS:
                add(niche if niche in QUERY_BANK else "", fp.format(kw=term))
    else:
        for n in niches:
            for q in QUERY_BANK[n]:
                add(n, q)
    return out


# ------------------------------------------------------------------ URL extraction

URL_RE = re.compile(r"""https?://[^\s"'<>()\[\]{}|\\^`]+""", re.I)
BARE_DOMAIN_RE = re.compile(r"^(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?:[/?#]\S*)?$", re.I)
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•·>]+|\d{1,4}[.)])\s*")
TRAILING = ".,;:!?)]}'\"»›…"

# Search engines, CDNs and infrastructure: never a publisher you could pitch.
NON_PUBLISHER = {
    "bing.com", "duckduckgo.com", "yahoo.com", "yandex.com", "yandex.ru", "baidu.com", "ecosia.org", "startpage.com",
    "brave.com", "qwant.com", "ask.com", "aol.com", "googleusercontent.com", "gstatic.com", "googleapis.com",
    "googletagmanager.com", "google-analytics.com", "doubleclick.net", "googlesyndication.com", "gravatar.com",
    "cloudflare.com", "jsdelivr.net", "wp.com", "w3.org", "schema.org", "archive.org", "bit.ly", "t.co", "goo.gl",
    "microsoft.com", "live.com", "office.com", "translate.goog", "ampproject.org", "feedburner.com",
}


def unwrap_redirect(url: str) -> str:
    """Search-engine result links often point at a redirect. Return the real target."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host, qs = (p.hostname or "").lower(), parse_qs(p.query)
    if re.match(r"^(www\.)?google\.[a-z.]+$", host) and p.path in ("/url", "/imgres"):
        for key in ("q", "url", "imgrefurl"):
            if qs.get(key) and qs[key][0].startswith("http"):
                return qs[key][0]
    if host.endswith("duckduckgo.com") and p.path.startswith("/l/") and qs.get("uddg"):
        return qs["uddg"][0]
    if host.endswith("bing.com") and p.path == "/ck/a" and qs.get("u"):
        u = qs["u"][0]
        if u.startswith("a1"):
            try:
                raw = u[2:]
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
            except (ValueError, UnicodeError):
                return url
    if host.endswith("search.yahoo.com"):
        m = re.search(r"/RU=([^/]+)/", p.path)
        if m:
            return unquote(m.group(1))
    return url


def _is_search_engine(domain: str) -> bool:
    return bool(re.match(r"^google\.[a-z.]+$", domain))


def skip_reason(domain: str) -> str:
    if _is_search_engine(domain) or domain in NON_PUBLISHER:
        return "Search engine or web infrastructure, not a publisher"
    if blocked_domain(domain):
        return "Platform, social network or shared host (not a single publisher)"
    return ""


def _tidy(url: str) -> str:
    return url.rstrip(TRAILING)


def extract_urls(text: str) -> tuple[list[str], set[str]]:
    """All http(s) URLs in pasted text or an HTML page, in order of appearance.
    Also returns the root domains of the page itself (for a saved HTML page), which are not
    candidates: they're the search engine or list site you saved."""
    text = text or ""
    found: list[str] = []
    own: set[str] = set()
    low = text[:5000].lower()
    if "<a " in low or "<html" in low or "<!doctype html" in low or "href=" in low:
        soup = BeautifulSoup(text, "html.parser")
        base = ""
        for tag, attr in (("link", "href"), ("meta", "content")):
            for el in soup.find_all(tag):
                if (tag == "link" and "canonical" in (el.get("rel") or [])) or (tag == "meta" and el.get("property") == "og:url"):
                    if el.get(attr, "").startswith("http"):
                        base = base or el[attr]
                        own.add(root_domain(el[attr]))
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/url?") or href.startswith("/l/?"):  # Google / DuckDuckGo relative redirects
                href = urljoin("https://www.google.com" if href.startswith("/url?") else "https://duckduckgo.com", href)
            elif base and not href.lower().startswith(("http:", "https:")):
                continue  # relative link inside the saved page itself
            if href.lower().startswith(("http:", "https:")):
                found.append(href)
        text = soup.get_text("\n")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        hits = URL_RE.findall(line)
        if hits:
            found += [_tidy(h) for h in hits]
            continue
        candidate = _tidy(LIST_MARKER_RE.sub("", line).split()[0]) if line.split() else ""
        if len(line.split()) == 1 or LIST_MARKER_RE.match(line):
            if candidate and BARE_DOMAIN_RE.match(candidate) and not re.search(r"\.(txt|csv|html?|pdf|png|jpe?g|js|py|exe|zip)$", candidate, re.I):
                found.append("https://" + candidate)
    return found, {d for d in own if d}


def plan_import(text: str) -> dict:
    """Turn raw text into one URL per root domain, plus what was skipped and why. No database access."""
    raw, own = extract_urls(text)
    by_domain: dict[str, str] = {}
    invalid, skipped, dup_in_input = 0, {}, 0
    for u in raw:
        url = clean_url(unwrap_redirect(u))
        domain = root_domain(url)
        if not url or not domain or re.fullmatch(r"[\d.]+", domain) or "." not in domain:
            invalid += 1
            continue
        if domain in own:
            skipped.setdefault(domain, "The page you saved or copied from (not a result)")
            continue
        reason = skip_reason(domain)
        if reason:
            skipped.setdefault(domain, reason)
            continue
        if domain in by_domain:
            dup_in_input += 1
            # Prefer a URL that already looks like the guidelines page.
            if not GUIDELINE_RE.search(urlparse(by_domain[domain]).path) and GUIDELINE_RE.search(urlparse(url).path):
                by_domain[domain] = url
            continue
        by_domain[domain] = url
    return {"urls": by_domain, "found": len(raw), "invalid": invalid, "skipped": skipped, "duplicates_in_input": dup_in_input}


# ------------------------------------------------------------------ queue

def import_urls(text: str, niche: str = "", query: str = "", source: str = "paste") -> dict:
    """Add the websites found in `text` to the import queue. Websites already in the
    database or already queued are reported, never added twice. Nothing is fetched here."""
    niche = niche if niche in NICHE_LABELS else ""
    query = re.sub(r"\s+", " ", query or "").strip()[:300]
    plan = plan_import(text)
    queued, known, already_queued = [], [], []
    ts = db.now()
    with db.transaction() as conn:
        for domain, url in plan["urls"].items():
            w = db.row(conn, "SELECT id, discovery_status FROM websites WHERE domain=?", (domain,))
            if w:
                known.append({"domain": domain, "website_id": w["id"], "discovery_status": w["discovery_status"]})
                continue
            if db.row(conn, "SELECT id FROM import_queue WHERE domain=? AND status='pending'", (domain,)):
                already_queued.append(domain)
                continue
            cur = conn.execute("INSERT INTO import_queue(url, domain, niche, query, source, status, created_at) "
                               "VALUES (?,?,?,?,?,'pending',?)", (url, domain, niche, query, source[:120], ts))
            queued.append({"id": cur.lastrowid, "domain": domain, "url": url})
    return {
        "found_links": plan["found"], "websites_in_input": len(plan["urls"]), "queued": queued,
        "already_known": known, "already_queued": already_queued, "duplicates_in_input": plan["duplicates_in_input"],
        "skipped": [{"domain": d, "reason": r} for d, r in plan["skipped"].items()], "invalid": plan["invalid"],
    }


def pending_candidates() -> list[dict]:
    """Pending imports as discovery candidates (oldest first)."""
    with db.transaction() as conn:
        rows = db.rows(conn, "SELECT * FROM import_queue WHERE status='pending' ORDER BY id")
    return [{"url": r["url"], "title": "", "snippet": "", "niche": r["niche"], "queue_id": r["id"],
             "query": r["query"] or "Imported URL (free discovery)"} for r in rows]


def mark(queue_id: int | None, status: str, note: str = "", website_id: int | None = None, run_id: int | None = None) -> None:
    if not queue_id:
        return
    with db.transaction() as conn:
        conn.execute("UPDATE import_queue SET status=?, note=?, website_id=COALESCE(?, website_id), run_id=?, processed_at=? "
                     "WHERE id=?", (status, note[:500], website_id, run_id, db.now(), queue_id))


def queue_status(limit: int = 30) -> dict:
    with db.transaction() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM import_queue GROUP BY status")}
        recent = db.rows(conn, "SELECT q.id, q.url, q.domain, q.niche, q.query, q.source, q.status, q.note, q.website_id, "
                               "q.created_at, q.processed_at, w.discovery_status, w.email, w.guidelines_url "
                               "FROM import_queue q LEFT JOIN websites w ON w.id=q.website_id ORDER BY q.id DESC LIMIT ?", (limit,))
    return {"counts": counts, "pending": counts.get("pending", 0), "recent": recent}
