"""Zero-cost discovery workflow: provider fallback, query generation, URL import,
deduplication, inspection of imported sites, 'Not Checked' metrics/email, and safety.
All network traffic is mocked; nothing here touches the internet or sends email."""
import base64
import csv
from urllib.parse import quote, quote_plus

import pytest

from app import config, db, discovery, email_verify, free_discovery, search_providers, seo_metrics, websites

from .test_discovery import RESULTS, crawler

PASTE = "\n".join(r["url"] for r in RESULTS)


@pytest.fixture
def free(client, tmp_path, monkeypatch):
    """The user's real situation: SEARCH_PROVIDER=brave with no key, no DA/DR or email services."""
    search_providers.set_provider_override(None)
    seo_metrics.set_override("moz", None)
    seo_metrics.set_override("ahrefs", None)
    email_verify.set_override(None)
    for k, v in {"SEARCH_PROVIDER": "brave", "BRAVE_API_KEY": "", "GOOGLE_CSE_API_KEY": "", "GOOGLE_CSE_ID": "",
                 "SEARXNG_URL": "", "MOZ_API_TOKEN": "", "AHREFS_API_KEY": "", "EMAIL_VERIFY_PROVIDER": "none"}.items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(config, "OUTREACH_CSV_PATH", tmp_path / "output" / "niche_websites_outreach.csv")
    yield
    search_providers.set_provider_override(None)


def run():
    return discovery.run_discovery("manual", force=True, crawler=crawler())


def by_domain():
    return {w["domain"]: w for w in websites.list_websites()}


def queue_rows():
    with db.transaction() as conn:
        return {r["domain"]: r for r in db.rows(conn, "SELECT * FROM import_queue ORDER BY id")}


# ------------------------------------------------------------------ provider selection: never paid by default

def test_missing_brave_key_falls_back_to_free_manual_mode(free):
    p, m = search_providers.resolve_provider()
    assert p.name == "manual" and p.automatic is False
    assert m["active"] == "manual" and m["automatic"] is False and m["paid"] is False
    assert "BRAVE_API_KEY" in m["reason"] and "free" in m["reason"].lower()


@pytest.mark.parametrize("provider,extra,active,automatic,paid", [
    ("manual", {}, "manual", False, False),
    ("none", {}, "manual", False, False),
    ("google_cse", {}, "manual", False, False),
    ("searxng", {}, "manual", False, False),
    ("nonsense", {}, "manual", False, False),
    ("searxng", {"SEARXNG_URL": "http://127.0.0.1:8080"}, "searxng", True, False),
    ("brave", {"BRAVE_API_KEY": "user-supplied-key"}, "brave", True, True),  # only if YOU add a key, and it's flagged paid
])
def test_provider_resolution(free, monkeypatch, provider, extra, active, automatic, paid):
    monkeypatch.setattr(config, "SEARCH_PROVIDER", provider)
    for k, v in extra.items():
        monkeypatch.setattr(config, k, v)
    _, m = search_providers.resolve_provider()
    assert (m["active"], m["automatic"], m["paid"]) == (active, automatic, paid)
    assert m["reason"]


def test_default_search_provider_is_free_manual():
    import importlib
    src = importlib.util.find_spec("app.config").origin
    assert '_env("SEARCH_PROVIDER", "manual")' in open(src, encoding="utf-8").read()


def test_free_mode_never_calls_a_search_api(free, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no search API may be called in free mode")
    monkeypatch.setattr(search_providers.BraveSearch, "search", boom)
    monkeypatch.setattr(search_providers.SearchProvider, "_get", boom)
    r = run()
    assert r["status"] == "completed" and r["usage"]["searches"] == 0
    assert r["summary"]["method"] == "manual" and r["summary"]["automatic_search"] is False
    assert any("BRAVE_API_KEY" in n for n in r["notices"])
    assert not any("BRAVE" in e for e in r["errors"])  # explained as a notice, not reported as a failure


# ------------------------------------------------------------------ query generation

def test_generated_queries_are_niche_specific_with_browser_links():
    qs = free_discovery.generate_queries("biometrics")
    assert qs and all(q["niche"] == "biometrics" for q in qs)
    assert any("biometric" in q["query"].lower() for q in qs)
    q = qs[0]
    assert set(q["links"]) == {"Google", "Bing", "DuckDuckGo", "Brave"}
    assert q["links"]["Google"] == "https://www.google.com/search?q=" + quote_plus(q["query"])
    all_q = free_discovery.generate_queries("")
    assert {q["niche"] for q in all_q} == {"education", "technology", "biometrics", "business_services"}


def test_custom_keywords_apply_every_footprint_without_duplicates():
    qs = free_discovery.generate_queries("education", "smart classroom, LMS, lms, ")
    texts = [q["query"] for q in qs]
    assert len(texts) == 2 * len(free_discovery.FOOTPRINTS) == len(set(texts))
    assert '"smart classroom" "write for us"' in texts and "LMS inurl:write-for-us" in texts


# ------------------------------------------------------------------ URL extraction

def test_unwrap_search_engine_redirects():
    target = "https://site.com/write-for-us"
    assert free_discovery.unwrap_redirect(f"https://www.google.com/url?q={quote(target)}&sa=U") == target
    assert free_discovery.unwrap_redirect(f"https://duckduckgo.com/l/?uddg={quote(target)}&rut=x") == target
    b = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert free_discovery.unwrap_redirect(f"https://www.bing.com/ck/a?!&&p=abc&u=a1{b}&ntb=1") == target
    assert free_discovery.unwrap_redirect(f"https://r.search.yahoo.com/_ylt=x/RU={quote(target, safe='')}/RK=2/RS=y") == target
    assert free_discovery.unwrap_redirect(target) == target


def test_pasted_search_results_are_parsed_and_deduplicated_by_root_domain():
    text = """
    BioSecure Blog
    https://www.biosecure-blog.com › blog › news
    Write for Us - BioSecure Blog https://biosecure-blog.com/write-for-us/?utm_source=google.
    1. edtech-daily.org
    - b2b-notes.co.uk/contribute
    https://www.google.com/search?q=more
    https://medium.com/@x/write-for-us
    Node.js is great, see readme.txt
    https://192.168.0.1/admin
    """
    plan = free_discovery.plan_import(text)
    assert plan["urls"] == {
        "biosecure-blog.com": "https://biosecure-blog.com/write-for-us",  # the guidelines URL wins over the homepage
        "edtech-daily.org": "https://edtech-daily.org/",
        "b2b-notes.co.uk": "https://b2b-notes.co.uk/contribute",
    }
    assert plan["duplicates_in_input"] == 1
    assert set(plan["skipped"]) == {"google.com", "medium.com"}
    assert plan["invalid"] == 1  # the IP address


def test_saved_results_html_page_is_parsed():
    html = """<!doctype html><html><head><link rel="canonical" href="https://guestpostlists.net/tech-blogs"></head><body>
      <a href="/url?q=https://edu-blog.com/write-for-us&sa=U">Edu Blog</a>
      <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftech-zone.io%2Fcontribute">Tech Zone</a>
      <a href="https://guestpostlists.net/about">About this list</a>
      <a href="/privacy">Privacy</a>
      <a href="https://twitter.com/share">Share</a>
      <a href="mailto:x@y.com">mail</a>
    </body></html>"""
    plan = free_discovery.plan_import(html)
    assert set(plan["urls"]) == {"edu-blog.com", "tech-zone.io"}
    assert "guestpostlists.net" in plan["skipped"] and "twitter.com" in plan["skipped"]


# ------------------------------------------------------------------ queue + inspection

def test_imported_urls_are_inspected_for_guidelines_contacts_articles_and_emails(free):
    r = free_discovery.import_urls(PASTE, niche="", query='biometrics "write for us"')
    assert {q["domain"] for q in r["queued"]} == {"biosecure-blog.com", "cheap-links-hub.net", "tasty-recipes-kitchen.com",
                                                   "edu-insights.org", "b2b-growth-notes.com", "private-tech-site.com",
                                                   "example-list.com"}
    assert {s["domain"] for s in r["skipped"]} == {"medium.com"}
    assert r["duplicates_in_input"] == 1  # biosecure-blog.com appeared twice

    out = run()
    assert out["status"] == "completed", out["errors"]
    s = by_domain()
    bio = s["biosecure-blog.com"]
    assert bio["discovery_status"] == "Qualified"
    assert bio["email"] == "editor@biosecure-blog.com" and bio["contact_source_url"].endswith("/write-for-us")
    assert bio["guidelines_url"].endswith("/write-for-us") and bio["accepts_guest_posts"] == "Yes"
    assert "fingerprint-scanners" in bio["article_url"] and bio["article_retrieved"] == 1
    assert bio["discovery_query"] == 'biometrics "write for us"'
    assert bio["email_verification"] == "Not Checked"
    assert bio["da"] is None and bio["dr"] is None
    assert s["b2b-growth-notes.com"]["email"] == ""  # nothing invented
    assert s["cheap-links-hub.net"]["discovery_status"] == "Rejected"
    assert "robots.txt" in s["private-tech-site.com"]["review_reason"]

    q = queue_rows()
    assert q["biosecure-blog.com"]["status"] == "processed" and q["biosecure-blog.com"]["website_id"] == bio["id"]
    assert all(r["status"] != "pending" for r in q.values())
    assert out["summary"]["imported_processed"] == 7 and out["summary"]["imports_pending"] == 0
    assert "could not be fetched" in s["example-list.com"]["review_reason"].lower() or s["example-list.com"]["discovery_status"] == "Needs Review"
    assert out["usage"]["searches"] == 0


def test_metrics_and_email_stay_not_checked_in_csv_and_api(free, client):
    free_discovery.import_urls(PASTE)
    run()
    rows = list(csv.DictReader(open(config.OUTREACH_CSV_PATH, encoding="utf-8-sig")))
    bio = next(x for x in rows if "biosecure" in x["Website URL"])
    assert (bio["DA"], bio["DR"], bio["DA Source"], bio["DR Source"]) == ("Not Checked",) * 4
    assert bio["Email Verified"] == "Not Checked"
    assert all(x["DA"] in ("Not Checked",) for x in rows)  # no number appears from nowhere
    st = client.get("/api/discovery/status").json()
    assert st["config"]["method"]["active"] == "manual"
    assert "Not Checked" in st["stats"]["verify_statuses"]


def test_reimport_never_duplicates_websites_or_queue_entries(free):
    first = free_discovery.import_urls("https://biosecure-blog.com/write-for-us\nhttps://b2b-growth-notes.com/contribute")
    assert len(first["queued"]) == 2
    again = free_discovery.import_urls("https://www.biosecure-blog.com/")
    assert again["queued"] == [] and again["already_queued"] == ["biosecure-blog.com"]
    run()
    later = free_discovery.import_urls("http://blog.biosecure-blog.com/x\nb2b-growth-notes.com")
    assert later["queued"] == [] and {k["domain"] for k in later["already_known"]} == {"biosecure-blog.com", "b2b-growth-notes.com"}
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM websites WHERE domain='biosecure-blog.com'").fetchone()[0] == 1


def test_import_limit_per_run_leaves_the_rest_pending(free, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_IMPORT_MAX_PER_RUN", 2)
    free_discovery.import_urls(PASTE)
    r1 = run()
    assert r1["summary"]["imported_processed"] == 2 and r1["summary"]["imports_pending"] == 5
    r2 = run()
    assert r2["summary"]["imported_processed"] == 2
    statuses = [r["status"] for r in queue_rows().values()]
    assert statuses.count("pending") == 3


def test_existing_records_are_preserved(free, client, provider):
    from .conftest import approve, import_csv, prepare_and_confirm
    import_csv(client, ["BioSecure,https://biosecure-blog.com,Maria,maria@biosecure-blog.com,Biometrics,,,my own note,new,yes"])
    cid = client.get("/api/contacts").json()[0]["id"]
    ids = client.post("/api/drafts/generate", json={"ids": [cid]}).json()["created"]
    approve(client, ids)
    prepare_and_confirm(client, ids)
    with db.transaction() as conn:
        websites.sync_from_contacts(conn)
    r = free_discovery.import_urls(PASTE)
    assert "biosecure-blog.com" in {k["domain"] for k in r["already_known"]}
    run()
    c = client.get("/api/contacts").json()[0]
    assert c["notes"] == "my own note" and c["email"] == "maria@biosecure-blog.com" and c["outreach_status"] == "contacted"
    w = by_domain()["biosecure-blog.com"]
    assert w["contact_id"] == cid and w["email"] == "maria@biosecure-blog.com" and w["outreach_label"] == "Sent"
    assert len(provider.sent) == 1  # only the email you confirmed yourself


def test_import_and_inspection_never_draft_or_send(free, client, provider):
    free_discovery.import_urls(PASTE)
    run()
    with db.transaction() as conn:
        for table in ("drafts", "contacts", "send_log", "send_batches"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert provider.sent == []


# ------------------------------------------------------------------ API

def test_import_api_paste_file_queue_and_queries(free, client):
    r = client.post("/api/discovery/import", json={"text": PASTE, "niche": "biometrics", "query": "q1", "process": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["queued"]) == 7 and body["run"]["started"] is False and body["pending"] == 7
    assert queue_rows()["biosecure-blog.com"]["niche"] == "biometrics"

    html = b'<html><body><a href="/url?q=https://new-edu-site.com/write-for-us">x</a></body></html>'
    r = client.post("/api/discovery/import-file", files={"file": ("results.html", html, "text/html")},
                    data={"niche": "education", "query": "", "process": "false"})
    assert r.status_code == 200, r.text
    assert [q["domain"] for q in r.json()["queued"]] == ["new-edu-site.com"]

    assert client.post("/api/discovery/import", json={"text": "  "}).status_code == 400
    assert client.post("/api/discovery/import-file", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400

    q = client.get("/api/discovery/import-queue").json()
    assert q["pending"] == 8 and q["recent"][0]["domain"] == "new-edu-site.com"
    qs = client.get("/api/discovery/queries", params={"niche": "technology", "keywords": "cloud security"}).json()
    assert qs and all("cloud security" in x["query"] for x in qs)
    st = client.get("/api/discovery/status").json()
    assert st["imports"]["pending"] == 8 and st["config"]["method"]["automatic"] is False


def test_import_api_starts_background_inspection(free, client, monkeypatch):
    started = []
    monkeypatch.setattr(discovery, "run_in_background", lambda trigger, force: started.append((trigger, force))
                        or {"started": True, "message": "Website discovery started."})
    r = client.post("/api/discovery/import", json={"text": "https://biosecure-blog.com/"}).json()
    assert r["run"]["started"] and started == [("import", True)]
    started.clear()
    r = client.post("/api/discovery/import", json={"text": "https://biosecure-blog.com/"}).json()  # nothing new
    assert started == [] and r["run"]["started"] is False


def test_imported_metrics_accept_only_real_values_for_known_sites(free):
    free_discovery.import_urls(PASTE)
    run()
    src = "Domain Rating by Ahrefs (free lookup)"
    r = seo_metrics.import_values({"biosecure-blog.com/": 55.0, "b2b-growth-notes.com": 150, "edu-insights.org": "abc",
                                   "never-seen.com": 40}, "dr", src)
    assert r["updated"] == 1 and r["not_in_database"] == ["never-seen.com"] and len(r["invalid"]) == 2
    s = by_domain()
    assert s["biosecure-blog.com"]["dr"] == 55.0 and s["biosecure-blog.com"]["dr_source"] == src
    assert s["biosecure-blog.com"]["da"] is None  # DA untouched
    assert s["b2b-growth-notes.com"]["dr"] is None  # out-of-range value refused, stays Not Checked
    assert "never-seen.com" not in s
    with pytest.raises(ValueError):
        seo_metrics.import_values({"biosecure-blog.com": 1}, "dr", " ")


def test_run_button_explains_empty_queue_in_free_mode(free, client, monkeypatch):
    started = []
    monkeypatch.setattr(discovery, "run_in_background", lambda trigger, force: started.append(trigger)
                        or {"started": True, "message": "Website discovery started."})
    r = client.post("/api/discovery/run").json()
    assert r["started"] is False and r["nothing_to_do"] and "Import website URLs" in r["message"]
    assert started == []  # no pointless run
    free_discovery.import_urls("https://biosecure-blog.com/")
    r = client.post("/api/discovery/run").json()
    assert r["started"] and r["pending"] == 1 and started == ["manual"]


def test_cli_import_and_queries(free, tmp_path, capsys):
    f = tmp_path / "urls.txt"
    f.write_text("https://biosecure-blog.com/write-for-us\nedu-insights.org\n", encoding="utf-8")
    assert discovery.main(["import", str(f), "--niche", "education"]) == 0
    assert "Queued 2 new website(s)" in capsys.readouterr().out
    assert discovery.main(["queries", "--niche", "education"]) == 0
    assert "google.com/search?q=" in capsys.readouterr().out
