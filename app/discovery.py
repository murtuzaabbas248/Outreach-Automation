"""Website discovery pipeline.

    (imported URLs from the free workflow | optional automatic search) -> normalize & deduplicate -> crawl politely -> qualify -> topic & notes
    -> verify emails -> DA / DR -> save -> export CSV

Discovery NEVER creates drafts or sends email. It doesn't import the sending
code at all. Discovered websites reach outreach only when you click
"Add to outreach contacts", and those contacts start unverified.

Run from the terminal:
    python -m app.discovery run                      # run now (even if it already ran today)
    python -m app.discovery run --if-not-run-today   # what the VS Code task / Task Scheduler use
    python -m app.discovery status
"""
import argparse
import json
import threading
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import config, csv_sync, db, email_verify, free_discovery, seo_metrics
from .crawler import Blocked, Crawler, FetchError
from .domains import clean_url, homepage, root_domain
from .qualify import (NICHE_LABELS, ParsedPage, acceptance, assess, best_article, blocked_domain, choose_email,
                      extract_contact_name, extract_emails, find_contact_links, find_guideline_links, is_aggregator,
                      looks_like_guidelines, primary_niche, site_name, spam_signals)
from .search_providers import SearchError, resolve_provider
from .templates import AUDIENCE
from .topics import detect_niche, focus_from_url, suggest_topics
from .websites import insert_website, merge_website, sync_from_contacts

STALE_MINUTES = 10  # a 'running' run with no heartbeat for this long was interrupted

QUERY_BANK = {
    "education": [
        'education blog "write for us"', 'educational technology "guest post"', 'school management "contribute"',
        'e-learning "submit an article"', 'education "write for us"', 'edtech "guest post guidelines"',
        'teaching blog "contributor guidelines"', 'higher education "become a contributor"',
        'online learning blog "accepting guest posts"', 'education blog "submit a guest post"',
        'teachers blog "contribute to us"', 'education "editorial guidelines" contributors',
    ],
    "technology": [
        'technology blog "write for us"', 'software technology "guest post"', 'IT blog "contributor guidelines"',
        'digital innovation "submit an article"', 'technology "write for us"', 'tech blog "become a contributor"',
        'SaaS blog "guest post guidelines"', 'cybersecurity blog "write for us"', 'AI technology "contribute to us"',
        'cloud computing blog "guest article"', 'technology "editorial guidelines" contributors',
        'tech news "accepting guest posts"',
    ],
    "biometrics": [
        'biometrics "write for us"', 'biometric technology "guest post"', 'fingerprint recognition technology blog contributor',
        'access control technology "submit an article"', 'RFID technology guest article', 'biometric technology guest post',
        'identity verification blog "write for us"', 'security technology blog "contributor guidelines"',
        'facial recognition blog "guest post"', 'time and attendance blog "write for us"',
        'physical security blog "contribute"', 'identity management "guest post guidelines"',
    ],
    "business_services": [
        'business services "write for us"', 'B2B business blog "guest post"', 'business consulting "contribute"',
        'business growth blog "submit an article"', 'business "write for us"', 'small business blog "guest post guidelines"',
        'entrepreneur blog "become a contributor"', 'B2B marketing "contributor guidelines"',
        'business blog "accepting guest posts"', 'outsourcing blog "write for us"',
        'business "editorial guidelines" expert contributors', 'business blog "submit a guest post"',
    ],
}

_thread_lock = threading.Lock()
_current: dict = {"run_id": None}


def all_queries() -> list[tuple[str, str]]:
    """Round-robin across niches so every niche is searched even with a small budget."""
    out, longest = [], max(len(v) for v in QUERY_BANK.values())
    for i in range(longest):
        for niche, qs in QUERY_BANK.items():
            if i < len(qs):
                out.append((niche, qs[i]))
    return out


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ run bookkeeping

def recover_interrupted(conn) -> int:
    """Runs left 'running' without a recent heartbeat were interrupted (app closed, crash, power loss).
    Everything they saved is kept: each website was committed on its own, so the next run carries on."""
    cur = conn.execute("UPDATE discovery_runs SET status='interrupted', finished_at=? WHERE status='running' AND heartbeat_at < ?",
                       (db.now(), _ago(STALE_MINUTES / 1440)))
    return cur.rowcount


def completed_today(conn) -> dict | None:
    return db.row(conn, "SELECT * FROM discovery_runs WHERE run_date=? AND status='completed' ORDER BY id DESC LIMIT 1",
                  (db.local_today(),))


def begin_run(trigger: str, force: bool) -> tuple[int | None, str]:
    """Create a run row, or explain why not. Never starts two runs at once."""
    with db.transaction() as conn:
        recover_interrupted(conn)
        running = db.row(conn, "SELECT id FROM discovery_runs WHERE status='running' ORDER BY id DESC LIMIT 1")
        if running:
            return None, f"Discovery run #{running['id']} is already in progress."
        if not force and completed_today(conn):
            return None, "Discovery already completed today; it will run again tomorrow (or click Run Website Discovery Now)."
        ts = db.now()
        cur = conn.execute("INSERT INTO discovery_runs(run_date, trigger, status, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
                           (db.local_today(), trigger, "running", ts, ts))
        return cur.lastrowid, "started"


def _heartbeat(run_id: int, summary: dict | None = None) -> None:
    with db.transaction() as conn:
        if summary is None:
            conn.execute("UPDATE discovery_runs SET heartbeat_at=? WHERE id=?", (db.now(), run_id))
        else:
            conn.execute("UPDATE discovery_runs SET heartbeat_at=?, summary=? WHERE id=?", (db.now(), json.dumps(summary), run_id))


def _finish(run_id: int, status: str, summary: dict, errors: list, usage: dict) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE discovery_runs SET status=?, finished_at=?, heartbeat_at=?, summary=?, errors=?, api_usage=? WHERE id=?",
                     (status, db.now(), db.now(), json.dumps(summary), json.dumps([config.redact(e) for e in errors][:200]),
                      json.dumps(usage), run_id))


def searches_this_month(conn) -> int:
    month = db.local_today()[:7]
    total = 0
    for r in conn.execute("SELECT api_usage FROM discovery_runs WHERE substr(run_date,1,7)=?", (month,)):
        total += int(json.loads(r["api_usage"] or "{}").get("searches", 0))
    return total


# ------------------------------------------------------------------ search phase

def collect_candidates(provider, usage: dict, errors: list, run_id: int) -> list[dict]:
    if not getattr(provider, "automatic", True):
        return []  # free manual mode: candidates come from the import queue instead
    queries = all_queries()
    with db.transaction() as conn:
        cache = {r["query"]: r for r in db.rows(conn, "SELECT * FROM search_cache WHERE provider=?", (provider.name,))}
        month_used = searches_this_month(conn)
    fresh_cutoff = _ago(config.DISCOVERY_SEARCH_CACHE_DAYS)
    # Queries never searched (or with the oldest cache) first.
    queries.sort(key=lambda nq: cache[nq[1]]["fetched_at"] if nq[1] in cache else "")
    budget = min(config.DISCOVERY_MAX_SEARCHES, max(config.DISCOVERY_MAX_SEARCHES_MONTH - month_used, 0))
    problem = provider.config_problem()
    if problem:
        errors.append(f"Search: {problem}")
    candidates = []
    for niche, q in queries:
        c = cache.get(q)
        if c and c["fetched_at"] >= fresh_cutoff:
            usage["cached_queries"] += 1
            results = json.loads(c["results"])
        elif not problem and usage["searches"] < budget:
            try:
                results = provider.search(q, config.DISCOVERY_RESULTS_PER_QUERY)
                usage["searches"] += 1
            except SearchError as exc:
                usage["searches"] += 1
                errors.append(f"Search '{q}': {exc}")
                if exc.quota:
                    problem = str(exc)
                continue
            with db.transaction() as conn:
                conn.execute("INSERT INTO search_cache(provider, query, niche, results, fetched_at) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(provider, query) DO UPDATE SET results=excluded.results, fetched_at=excluded.fetched_at",
                             (provider.name, q, niche, json.dumps(results), db.now()))
            _heartbeat(run_id)
        else:
            continue
        for r in results:
            candidates.append({**r, "niche": niche, "query": q})
    if usage["searches"] >= budget and budget < len(queries):
        usage["search_budget_reached"] = True
    return candidates


# ------------------------------------------------------------------ per-domain analysis

def analyse_domain(domain: str, result_url: str, result_title: str, niche_hint: str, crawler: Crawler) -> dict:
    """Fetch a few pages of one website and return what was actually found on them."""
    pages: dict[str, ParsedPage] = {}
    blocked, notes = "", []

    def get(url: str) -> ParsedPage | None:
        nonlocal blocked
        url = clean_url(url)
        if not url or url in pages:
            return pages.get(url)
        try:
            page = crawler.fetch(url)
        except Blocked as exc:
            blocked = blocked or str(exc)
            return None
        except FetchError as exc:
            notes.append(str(exc))
            return None
        if root_domain(page.final_url) != domain:
            notes.append(f"{url} redirected off-site to {page.final_url}")
            return None
        pages[url] = p = ParsedPage(page.final_url, page.html)
        return p

    res = get(result_url) if root_domain(result_url) == domain else None
    home = get(homepage(result_url) if root_domain(result_url) == domain else homepage(domain))
    if home is None and not blocked and homepage(result_url) != homepage(domain):
        home = get(homepage(domain))  # e.g. www.example.com failed; try example.com
    known = [p for p in (res, home) if p]
    if not known:
        return {"blocked": blocked or (notes[0] if notes else "Website could not be fetched"), "pages": []}

    guide = res if res and looks_like_guidelines(res) else None
    if guide is None:
        for p in known:
            for link in find_guideline_links(p, domain)[:2]:
                guide = get(link)
                if guide and looks_like_guidelines(guide):
                    break
                guide = None
            if guide:
                break

    email_candidates = []
    for p in pages.values():
        trusted = p is guide or bool(find_contact_links_match(p))
        email_candidates += [(e, p.url, trusted) for e in extract_emails(p)]
    if not any(t for *_, t in email_candidates):
        for link in find_contact_links(home or known[0], domain):
            cp = get(link)
            if cp:
                email_candidates += [(e, cp.url, True) for e in extract_emails(cp)]
                if extract_emails(cp):
                    break

    all_pages = list(pages.values())
    niche_key, niche_score, matched = primary_niche(all_pages, niche_hint)
    email = choose_email(email_candidates, domain)
    contact_name, contact_role = "", ""
    for p in ([guide] if guide else []) + [p for p in all_pages if p is not guide]:
        contact_name, contact_role = extract_contact_name(p)
        if contact_name:
            contact_source = p.url
            break
    else:
        contact_source = ""

    article_url, article_anchor = best_article(all_pages, domain, niche_key) if niche_key else ("", "")
    article_title, article_retrieved = "", False
    if article_url:
        ap = get(article_url)
        if ap:
            article_title = (ap.h1 or ap.title)[:200]
            article_retrieved = True
            article_url = ap.url
        elif blocked:
            article_url = ""  # couldn't confirm it exists
        else:
            article_title = article_anchor  # link text only; the page itself was not opened

    accepts, accept_evidence = acceptance(guide)
    spam, spam_words = spam_signals(all_pages)
    return {
        "blocked": "", "pages": all_pages, "home": home, "guide": guide, "niche_key": niche_key,
        "niche_score": niche_score, "matched_terms": matched, "email": email, "contact_name": contact_name,
        "contact_role": contact_role, "contact_name_source": contact_source, "article_url": article_url,
        "article_title": article_title, "article_retrieved": article_retrieved, "accepts": accepts,
        "accept_evidence": accept_evidence, "spam": spam, "spam_words": spam_words, "fetch_notes": notes[:5],
        "name": site_name(home, all_pages, domain),
        "canonical": (home.canonical if home and home.canonical and root_domain(home.canonical) == domain else ""),
    }


def find_contact_links_match(p: ParsedPage) -> bool:
    path = urlparse(p.url).path.lower()
    return any(w in path for w in ("contact", "about", "team", "editorial", "masthead"))


# ------------------------------------------------------------------ topic & personalization

def personalize(w: dict, a: dict) -> tuple[str, str]:
    """One topic and short notes, built only from facts collected on the site."""
    niche_key = a.get("niche_key", "")
    if not niche_key:
        return "", ""
    focus_src = a.get("article_url", "")
    seed_contact = {"email": w.get("email") or w["domain"], "niche": NICHE_LABELS[niche_key], "article_url": focus_src}
    topic_key = detect_niche(NICHE_LABELS[niche_key])
    topic = suggest_topics(seed_contact, count=1)[0]
    focus = focus_from_url(focus_src)

    notes = []
    terms = ", ".join(a.get("matched_terms", [])[:4])
    notes.append(f"Covers {NICHE_LABELS[niche_key].lower()}" + (f" (site mentions: {terms})." if terms else "."))
    home = a.get("home")
    if home and home.description:
        desc = home.description.strip()
        notes.append(f"Site describes itself as: \"{desc[:140]}{'…' if len(desc) > 140 else ''}\"")
    if a.get("article_url"):
        if a.get("article_retrieved") and a.get("article_title"):
            notes.append(f"Related article (page retrieved, not read in depth): \"{a['article_title'][:120]}\".")
        elif a.get("article_title"):
            notes.append(f"Related article link found (not opened): \"{a['article_title'][:120]}\".")
    if a.get("accepts") == "Yes" and a.get("guide"):
        notes.append("Has published contributor guidelines; follow them when pitching.")
    elif a.get("guide"):
        notes.append("Has a contributor/submission page, but acceptance isn't stated clearly; check before pitching.")
    audience = AUDIENCE.get(topic_key, "")
    if topic and audience:
        angle = f" and builds on their coverage of {focus}" if focus else ""
        notes.append(f"Pitch angle: the topic suits {audience}{angle}.")
    return topic, " ".join(notes)


# ------------------------------------------------------------------ main run

def run_discovery(trigger: str = "manual", force: bool = True, crawler: Crawler | None = None) -> dict:
    run_id, message = begin_run(trigger, force)
    if run_id is None:
        return {"started": False, "message": message}
    _current["run_id"] = run_id
    summary = {"new_websites": 0, "existing_websites": 0, "duplicate_results": 0, "qualified": 0, "needs_review": 0,
               "rejected": 0, "skipped_platforms": 0, "revisited": 0, "metrics_updated": 0, "emails_checked": 0,
               "imported_processed": 0, "imports_pending": 0, "method": "", "automatic_search": False,
               "notices": [], "new_domains": [], "csv": ""}
    usage = {"searches": 0, "cached_queries": 0, "page_requests": 0, "moz_lookups": 0, "ahrefs_requests": 0,
             "email_verifications": 0, "provider": ""}
    errors: list[str] = []
    try:
        with db.transaction() as conn:
            sync_from_contacts(conn)
        provider, method = resolve_provider()
        usage["provider"] = provider.name
        summary.update(method=method["active"], automatic_search=method["automatic"])
        if method["reason"] and not method["automatic"]:
            summary["notices"].append(method["reason"])
        # Websites you imported come first; then automatic search results (only if a search provider is set up).
        candidates = free_discovery.pending_candidates() + collect_candidates(provider, usage, errors, run_id)
        crawler = crawler or Crawler()
        process_candidates(candidates, crawler, summary, errors, run_id)
        usage["page_requests"] = crawler.total_requests
        verify_emails(summary, usage, errors)
        update_metrics(summary, usage, errors)
        summary["imports_pending"] = free_discovery.queue_status(limit=0)["pending"]
        try:
            exp = csv_sync.export()
            summary["csv"] = exp["path"]
            if exp["warning"]:
                errors.append(exp["warning"])
        except OSError as exc:
            errors.append(f"CSV export failed: {exc}")
        _finish(run_id, "completed", summary, errors, usage)
        return {"started": True, "run_id": run_id, "status": "completed", "summary": summary, "errors": errors, "usage": usage,
                "notices": summary["notices"]}
    except Exception as exc:  # record and keep everything already saved
        errors.append(f"Run failed: {type(exc).__name__}: {exc}")
        errors.append(traceback.format_exc(limit=3))
        _finish(run_id, "failed", summary, errors, usage)
        return {"started": True, "run_id": run_id, "status": "failed", "summary": summary, "errors": errors, "usage": usage}
    finally:
        _current["run_id"] = None


def process_candidates(candidates: list[dict], crawler: Crawler, summary: dict, errors: list, run_id: int) -> None:
    """Inspect each new root domain once. Candidates with a `queue_id` came from the free
    import queue; their outcome is written back to it. Imports over the per-run limit stay pending."""
    seen: set[str] = set()
    processed = {"search": 0, "import": 0}
    limits = {"search": config.DISCOVERY_DAILY_LIMIT, "import": config.DISCOVERY_IMPORT_MAX_PER_RUN}
    for cand in candidates:
        qid = cand.get("queue_id")
        kind = "import" if qid else "search"
        mark = lambda status, note="", wid=None: free_discovery.mark(qid, status, note, wid, run_id)  # noqa: E731
        url = clean_url(cand.get("url", ""))
        domain = root_domain(url)
        if not domain:
            mark("skipped", "Not a valid website URL")
            continue
        if domain in seen:
            summary["duplicate_results"] += 1
            mark("duplicate", "Same website appeared earlier in this run")
            continue
        seen.add(domain)
        if blocked_domain(domain) or is_aggregator(cand.get("title", "")):
            summary["skipped_platforms"] += 1
            mark("skipped", "Platform, social network or list page")
            continue
        with db.transaction() as conn:
            existing = db.row(conn, "SELECT * FROM websites WHERE domain=?", (domain,))
        if existing:
            revisit = (not existing["status_locked"] and existing["discovery_status"] in ("New", "Needs Review")
                       and (existing["crawled_at"] or "") < _ago(config.DISCOVERY_REVISIT_DAYS) and not existing["contact_id"])
            if not revisit:
                summary["existing_websites"] += 1
                mark("already_known", "Website is already in your database", existing["id"])
                continue
        if processed[kind] >= limits[kind]:
            seen.discard(domain)  # not inspected; an import stays pending for the next run
            continue
        processed[kind] += 1
        try:
            wid = save_analysis(domain, url, cand, analyse_domain(domain, url, cand.get("title", ""), cand.get("niche", ""), crawler),
                                existing, summary)
            mark("processed", "Inspected", wid)
            summary["imported_processed"] += kind == "import"
        except Exception as exc:  # one bad site must not stop the run
            errors.append(f"{domain}: {type(exc).__name__}: {exc}")
            mark("error", f"{type(exc).__name__}: {exc}")
        _heartbeat(run_id, summary)


def save_analysis(domain: str, url: str, cand: dict, a: dict, existing: dict | None, summary: dict) -> int:
    """Store what was found; returns the website id."""
    ts = db.now()
    if a["blocked"]:
        verdict = assess(niche_key="", niche_score=0, accepts="Unknown", accept_evidence=[], spam=False, spam_words=[],
                         email=None, guidelines_url="", blocked_reason=a["blocked"])
        data = {"domain": domain, "website_url": homepage(domain), "discovery_query": cand.get("query", ""), "source_url": url,
                "discovery_status": verdict["status"], "confidence": 0, "evidence": json.dumps(verdict["evidence"]),
                "review_reason": verdict["reason"], "crawled_at": ts, "last_checked": ts}
    else:
        guide = a["guide"]
        email = a["email"]
        verdict = assess(niche_key=a["niche_key"], niche_score=a["niche_score"], accepts=a["accepts"],
                         accept_evidence=a["accept_evidence"], spam=a["spam"], spam_words=a["spam_words"], email=email,
                         guidelines_url=guide.url if guide else "")
        evidence = verdict["evidence"] + ([f"Contact name '{a['contact_name']}' listed as {a['contact_role']} on {a['contact_name_source']}"]
                                          if a["contact_name"] else []) + a["fetch_notes"][:2]
        status, source = email_verify.local_status(email["email"] if email else "")
        data = {
            "domain": domain, "website_name": a["name"], "website_url": a["canonical"] or homepage(url),
            "contact_name": a["contact_name"], "email": email["email"] if email else "",
            "email_verification": status if email else "Unknown", "email_verification_source": source if email else "",
            "contact_source_url": email["source_url"] if email else "",
            "niche": NICHE_LABELS.get(a["niche_key"], ""), "article_url": a["article_url"], "article_title": a["article_title"],
            "article_retrieved": int(a["article_retrieved"]), "guidelines_url": guide.url if guide else "",
            "accepts_guest_posts": a["accepts"], "discovery_status": verdict["status"], "confidence": verdict["confidence"],
            "evidence": json.dumps(evidence), "review_reason": verdict["reason"], "discovery_query": cand.get("query", ""),
            "source_url": url, "crawled_at": ts, "last_checked": ts,
        }
        if verdict["status"] != "Rejected":
            topic, notes = personalize({**data, "domain": domain}, a)
            data.update(suggested_topic=topic, personalization_notes=notes)
    with db.transaction() as conn:
        if existing:
            merge_website(conn, db.row(conn, "SELECT * FROM websites WHERE id=?", (existing["id"],)), data, "Discovery re-check")
            summary["revisited"] += 1
            website_id = existing["id"]
        else:
            website_id = insert_website(conn, data)
            summary["new_websites"] += 1
            summary["new_domains"].append(domain)
    key = {"Qualified": "qualified", "Needs Review": "needs_review", "Rejected": "rejected"}.get(data["discovery_status"])
    if key:
        summary[key] += 1
    return website_id


def verify_emails(summary: dict, usage: dict, errors: list) -> None:
    verifier = email_verify.get_verifier()
    with db.transaction() as conn:
        todo = db.rows(conn, "SELECT id, email FROM websites WHERE email!='' AND discovery_status!='Rejected' "
                             "AND email_verification IN ('Unknown','Syntax Valid','Not Checked') ORDER BY discovered_at DESC")
    for w in todo:
        with db.transaction() as conn:
            hit = email_verify.cached(conn, w["email"])
        if hit is None and verifier is not None and usage["email_verifications"] < config.EMAIL_VERIFY_MAX_PER_RUN:
            try:
                status, raw = verifier.verify(w["email"])
                usage["email_verifications"] += 1
                with db.transaction() as conn:
                    hit = (status, email_verify.store(conn, w["email"], status, raw, verifier.name))
            except email_verify.VerifyError as exc:
                errors.append(f"Email verification: {exc}")
                if exc.quota:
                    verifier = None
                continue
        if hit:
            with db.transaction() as conn:
                conn.execute("UPDATE websites SET email_verification=?, email_verification_source=?, last_checked=?, updated_at=? "
                             "WHERE id=? AND email=?", (hit[0], hit[1], db.now(), db.now(), w["id"], w["email"]))
            summary["emails_checked"] += 1


def update_metrics(summary: dict, usage: dict, errors: list) -> None:
    cutoff = _ago(config.METRICS_REFRESH_DAYS)
    limit = config.METRICS_MAX_LOOKUPS_PER_RUN
    for kind, getter, col in (("moz", seo_metrics.get_moz, "da"), ("ahrefs", seo_metrics.get_ahrefs, "dr")):
        provider = getter()
        if provider is None:
            label = "DA" if kind == "moz" else "DR"
            summary.setdefault("notices", []).append(
                f"{label}: Not Checked. No reliable free {label} source is configured, so {label} is left as "
                f"Not Checked and never estimated.")
            continue
        with db.transaction() as conn:
            domains = [r["domain"] for r in conn.execute(
                f"SELECT domain FROM websites WHERE discovery_status IN ('Qualified','Needs Review') "
                f"AND ({col}_checked_at IS NULL OR {col}_checked_at < ?) ORDER BY discovery_status='Qualified' DESC, "
                f"discovered_at DESC LIMIT ?", (cutoff, limit))]
        if not domains:
            continue
        try:
            values = provider.fetch(domains)
        except seo_metrics.MetricError as exc:
            errors.append(f"{'DA' if kind == 'moz' else 'DR'}: {exc}")
            continue
        finally:
            usage["moz_lookups" if kind == "moz" else "ahrefs_requests"] += len(domains) if kind == "moz" else 1
        ts = db.now()
        with db.transaction() as conn:
            for d, v in values.items():
                source = provider.source if v is not None else f"{provider.source}: no value returned"
                conn.execute(f"UPDATE websites SET {col}=?, {col}_source=?, {col}_checked_at=?, updated_at=? WHERE domain=?",
                             (v, source, ts, ts, d))
                summary["metrics_updated"] += v is not None


# ------------------------------------------------------------------ startup / background

def run_in_background(trigger: str, force: bool) -> dict:
    """Start a run on a background thread. Returns immediately."""
    if not _thread_lock.acquire(blocking=False):
        return {"started": False, "message": "A discovery run is already in progress in this app."}
    with db.transaction() as conn:
        recover_interrupted(conn)
        if not force and completed_today(conn):
            _thread_lock.release()
            return {"started": False, "message": "Discovery already completed today."}
        if db.row(conn, "SELECT id FROM discovery_runs WHERE status='running'"):
            _thread_lock.release()
            return {"started": False, "message": "A discovery run is already in progress."}

    def _run():
        try:
            run_discovery(trigger, force)
        finally:
            _thread_lock.release()

    threading.Thread(target=_run, daemon=True, name="website-discovery").start()
    return {"started": True, "message": "Website discovery started."}


def on_startup() -> dict:
    """Called when the app starts: recover interrupted runs, then run once per local day."""
    with db.transaction() as conn:
        recovered = recover_interrupted(conn)
        # Runs started by the dashboard belonged to a previous app process, which is gone.
        # (Terminal / Task Scheduler runs may still be going, so those wait for the heartbeat timeout.)
        recovered += conn.execute("UPDATE discovery_runs SET status='interrupted', finished_at=? WHERE status='running' "
                                  "AND trigger IN ('startup','manual','import')", (db.now(),)).rowcount
    if not config.DISCOVERY_AUTO_ON_STARTUP:
        return {"started": False, "message": "Automatic discovery is off (DISCOVERY_AUTO_ON_STARTUP=false).", "recovered": recovered}
    result = run_in_background("startup", force=False)
    result["recovered"] = recovered
    return result


def status() -> dict:
    with db.transaction() as conn:
        recover_interrupted(conn)
        runs = db.rows(conn, "SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 15")
        for r in runs:
            for k in ("summary", "errors", "api_usage"):
                r[k] = json.loads(r[k] or ("[]" if k == "errors" else "{}"))
        return {
            "running": next((r for r in runs if r["status"] == "running"), None),
            "last_completed": next((r for r in runs if r["status"] == "completed"), None),
            "completed_today": bool(completed_today(conn)),
            "today": db.local_today(),
            "runs": runs,
            "searches_this_month": searches_this_month(conn),
            "config": config_status(),
            "imports": free_discovery.queue_status(limit=0),
        }


def config_status() -> dict:
    provider, method = resolve_provider()
    return {
        "method": method,
        "search_provider": provider.name, "search_problem": "" if method["automatic"] else method["reason"],
        "import_max_per_run": config.DISCOVERY_IMPORT_MAX_PER_RUN,
        "moz_configured": not seo_metrics.MozDA.config_problem(), "moz_problem": seo_metrics.MozDA.config_problem(),
        "ahrefs_configured": not seo_metrics.AhrefsDR.config_problem(), "ahrefs_problem": seo_metrics.AhrefsDR.config_problem(),
        "ahrefs_endpoint": config.AHREFS_DR_ENDPOINT,
        "email_verify_provider": config.EMAIL_VERIFY_PROVIDER, "email_verify_problem": email_verify.config_problem(),
        "auto_on_startup": config.DISCOVERY_AUTO_ON_STARTUP, "daily_limit": config.DISCOVERY_DAILY_LIMIT,
        "max_searches": config.DISCOVERY_MAX_SEARCHES, "max_searches_month": config.DISCOVERY_MAX_SEARCHES_MONTH,
        "max_requests_per_domain": config.DISCOVERY_MAX_REQUESTS_PER_DOMAIN, "csv_path": str(config.OUTREACH_CSV_PATH),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.discovery", description="Website discovery")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run discovery now")
    r.add_argument("--if-not-run-today", action="store_true", help="Skip if a run already completed today")
    # 'startup' and 'manual' are reserved for runs started inside the dashboard app.
    r.add_argument("--trigger", default="cli", choices=["cli", "scheduled", "vscode"])
    sub.add_parser("status", help="Show recent runs")
    sub.add_parser("export", help="Write the CSV from the database")
    q = sub.add_parser("queries", help="Print free search queries with browser search links")
    q.add_argument("--niche", default="", choices=["", *QUERY_BANK])
    q.add_argument("--keywords", default="", help="Comma-separated keywords of your own")
    im = sub.add_parser("import", help="Queue website URLs from a .txt/.csv/.html file (free discovery)")
    im.add_argument("file")
    im.add_argument("--niche", default="", choices=["", *QUERY_BANK])
    im.add_argument("--query", default="", help="The search these results came from")
    mt = sub.add_parser("import-metrics", help="Store DA/DR values from a real provider (JSON {domain: value})")
    mt.add_argument("file")
    mt.add_argument("--metric", required=True, choices=["da", "dr"])
    mt.add_argument("--source", required=True, help="Where the numbers came from, e.g. 'Domain Rating by Ahrefs (free)'")
    args = parser.parse_args(argv)
    db.init_db()
    if args.cmd == "import-metrics":
        from pathlib import Path
        data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
        r = seo_metrics.import_values(data, args.metric, args.source)
        print(f"{args.metric.upper()}: updated {r['updated']}, unchanged {r['unchanged']}, "
              f"not in database {len(r['not_in_database'])}, invalid {len(r['invalid'])}")
        csv_sync.export()
        return 0
    if args.cmd == "queries":
        for item in free_discovery.generate_queries(args.niche, args.keywords):
            print(f"[{item['niche_label'] or 'any'}] {item['query']}\n    {item['links']['Google']}\n    {item['links']['Bing']}")
        return 0
    if args.cmd == "import":
        from pathlib import Path
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        r = free_discovery.import_urls(text, args.niche, args.query, source=f"cli:{Path(args.file).name}")
        print(f"Queued {len(r['queued'])} new website(s); {len(r['already_known'])} already known, "
              f"{len(r['already_queued'])} already queued, {len(r['skipped'])} skipped, {r['invalid']} invalid.")
        print("Run `python -m app.discovery run` (or click Run in the dashboard) to inspect them.")
        return 0
    if args.cmd == "status":
        s = status()
        print(f"Today: {s['today']}  completed today: {s['completed_today']}  searches this month: {s['searches_this_month']}")
        for run in s["runs"][:10]:
            print(f"#{run['id']} {run['run_date']} {run['trigger']:<9} {run['status']:<11} "
                  f"new={run['summary'].get('new_websites', 0)} qualified={run['summary'].get('qualified', 0)}")
        return 0
    if args.cmd == "export":
        print(json.dumps(csv_sync.export(), indent=2))
        return 0
    lines = []
    out = lambda text: (print(text), lines.append(text))  # noqa: E731
    result = run_discovery(args.trigger, force=not args.if_not_run_today)
    if not result["started"]:
        out(result["message"])
    else:
        s = result["summary"]
        out(f"Run #{result['run_id']} ({args.trigger}): {result['status']}")
        out(f"  New websites: {s['new_websites']}  Existing (skipped): {s['existing_websites']}  "
            f"Duplicate results: {s['duplicate_results']}  Platforms skipped: {s['skipped_platforms']}")
        out(f"  Qualified: {s['qualified']}  Needs review: {s['needs_review']}  Rejected: {s['rejected']}")
        out(f"  Method: {s['method']}  Imported websites inspected: {s['imported_processed']}  Still pending: {s['imports_pending']}")
        out(f"  API usage: {json.dumps(result['usage'])}")
        out(f"  CSV: {s['csv']}")
        for n in s.get("notices", []):
            out(f"  i {n}")
        for e in result["errors"][:20]:
            out(f"  ! {config.redact(e.splitlines()[0]) if e else e}")
    try:  # a plain log next to the database, handy for unattended (Task Scheduler) runs
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.DATA_DIR / "discovery.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] " + "\n".join(lines) + "\n")
    except OSError:
        pass
    return 0 if not result["started"] or result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
