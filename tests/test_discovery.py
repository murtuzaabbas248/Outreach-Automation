"""Website discovery: search, crawling, qualification, metrics, verification,
CSV, daily scheduling, recovery and the link to the (safe) outreach workflow.
All network traffic is mocked; nothing here touches the internet or sends email."""
import csv
import io
import json

import httpx
import pytest

from app import config, csv_sync, db, discovery, email_verify, search_providers, seo_metrics, websites
from app.crawler import Crawler
from app.domains import clean_url, root_domain

from .conftest import approve, import_csv, prepare_and_confirm, set_settings

EXPECTED_HEADER = ["Website Name", "Website URL", "Contact Name", "Email", "Niche", "Relevant Article URL", "Suggested Topic",
                   "Personalization Notes", "Outreach Status", "Email Verified", "DA", "DR", "Guest Post Guidelines URL",
                   "Contact Source URL", "DA Source", "DR Source", "Last Checked", "Discovery Query", "Discovery Status"]


def page(title, body, desc="", site_name=""):
    meta = f'<meta name="description" content="{desc}">' if desc else ""
    og = f'<meta property="og:site_name" content="{site_name}">' if site_name else ""
    return f"<html><head><title>{title}</title>{meta}{og}</head><body>{body}</body></html>"


SITES = {
    # A relevant biometrics blog with a clear contributor page and editorial email.
    "https://biosecure-blog.com/": page(
        "BioSecure Blog | Biometric Security News",
        "<h1>Biometric security and access control</h1><h2>Fingerprint and facial recognition guides</h2>"
        "<a href='/write-for-us/'>Write for Us</a> <a href='/contact'>Contact</a>"
        "<a href='/fingerprint-scanners-small-office-security-guide/?utm_source=x'>How fingerprint scanners improve small office security</a>"
        "<p>Biometric devices, RFID access control and identity verification for businesses.</p>",
        desc="News and guides on biometric devices, fingerprint scanners and access control systems.", site_name="BioSecure Blog"),
    "https://biosecure-blog.com/write-for-us": page(
        "Write for Us - BioSecure Blog",
        "<h1>Write for Us</h1><p>We accept guest posts about biometrics, fingerprint recognition, facial recognition "
        "and access control. Read our contributor guidelines below.</p><p>Editor: Maria Lopez</p>"
        "<p>Send your pitch to <a href='mailto:editor@biosecure-blog.com'>editor@biosecure-blog.com</a>. "
        "For privacy questions: privacy@biosecure-blog.com</p>"),
    "https://biosecure-blog.com/fingerprint-scanners-small-office-security-guide": page(
        "How fingerprint scanners improve small office security", "<h1>How Fingerprint Scanners Improve Small-Office Security</h1>"
        "<p>Biometric fingerprint access control for offices.</p>"),
    # Link-selling spam.
    "https://cheap-links-hub.net/write-for-us": page(
        "Write for Us - Guest Post", "<h1>Write for us</h1><p>We accept all niches including casino, CBD and crypto. "
        "Buy guest post with dofollow link for $50. Technology, business, education welcome.</p>"
        "<a href='mailto:sales@cheap-links-hub.net'>sales@cheap-links-hub.net</a>"),
    "https://cheap-links-hub.net/": page("Cheap Links Hub", "<h1>Guest posts</h1>"),
    # Irrelevant niche.
    "https://tasty-recipes-kitchen.com/write-for-us": page(
        "Write for Us | Tasty Recipes", "<h1>Write for us</h1><p>Share your favourite recipes, soups and desserts. "
        "Email recipes@tasty-recipes-kitchen.com</p>"),
    "https://tasty-recipes-kitchen.com/": page("Tasty Recipes Kitchen", "<h1>Soups, stews and desserts</h1>"),
    # Relevant but explicitly closed to guest posts.
    "https://edu-insights.org/write-for-us": page(
        "Write for Us - Edu Insights", "<h1>Write for us</h1><p>Education, teachers and students. We no longer accept "
        "guest posts. Thank you for your interest.</p><a href='mailto:editor@edu-insights.org'>editor@edu-insights.org</a>"),
    "https://edu-insights.org/": page("Edu Insights | Education blog", "<h1>Education for teachers and students</h1>"
                                      "<p>School, classroom and university learning.</p>"),
    # Relevant site with no contact email anywhere.
    "https://b2b-growth-notes.com/contribute": page(
        "Contribute to Us - B2B Growth Notes", "<h1>Contribute to us</h1><p>We accept guest posts on B2B business services, "
        "consulting and small business growth. Read the contributor guidelines and use our form.</p>"),
    "https://b2b-growth-notes.com/": page("B2B Growth Notes", "<h1>B2B business services and consulting</h1>"
                                          "<p>Small business growth, outsourcing and B2B marketing.</p>"),
}
ROBOTS = {"https://private-tech-site.com/robots.txt": "User-agent: *\nDisallow: /\n"}

RESULTS = [
    {"url": "https://www.biosecure-blog.com/write-for-us/?utm_campaign=abc", "title": "Write for Us - BioSecure Blog", "snippet": ""},
    {"url": "https://biosecure-blog.com/contact", "title": "Contact", "snippet": ""},  # same domain again -> duplicate
    {"url": "https://cheap-links-hub.net/write-for-us", "title": "Write for us", "snippet": ""},
    {"url": "https://tasty-recipes-kitchen.com/write-for-us", "title": "Write for Us", "snippet": ""},
    {"url": "https://edu-insights.org/write-for-us", "title": "Write for Us", "snippet": ""},
    {"url": "https://b2b-growth-notes.com/contribute", "title": "Contribute", "snippet": ""},
    {"url": "https://private-tech-site.com/write-for-us", "title": "Write for us", "snippet": ""},
    {"url": "https://medium.com/@someone/write-for-us", "title": "Write for us", "snippet": ""},
    {"url": "https://example-list.com/guest-post-sites", "title": "500+ Sites That Accept Guest Posts in 2026", "snippet": ""},
]


def site_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url in ROBOTS:
        return httpx.Response(200, text=ROBOTS[url], headers={"content-type": "text/plain"})
    if url.endswith("/robots.txt"):
        return httpx.Response(404, text="")
    if "://www." in url:  # like most real sites, www. redirects to the bare domain
        return httpx.Response(301, headers={"location": url.replace("://www.", "://", 1)})
    key = clean_url(url)
    key = key + "/" if key.count("/") == 2 else key
    html = SITES.get(key) or SITES.get(key.rstrip("/")) or SITES.get(key.rstrip("/") + "/")
    if html is None:
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
    return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})


class FakeSearch(search_providers.SearchProvider):
    name = "fake"

    def __init__(self, results=None, error=None):
        self.results = RESULTS if results is None else results
        self.calls = []
        self.error = error

    def config_problem(self):
        return ""

    def search(self, query, count=20):
        self.calls.append(query)
        if self.error:
            raise self.error
        return list(self.results)


class FakeMetric:
    def __init__(self, name, source, values):
        self.name, self.source, self.values, self.calls = name, source, values, []

    def fetch(self, domains):
        self.calls.append(list(domains))
        return {d: seo_metrics.valid_metric(self.values.get(d)) for d in domains}


@pytest.fixture
def disc(client, tmp_path, monkeypatch):
    """A test app with fake search + mocked websites, no metric/verification providers."""
    fake = FakeSearch()
    search_providers.set_provider_override(fake)
    seo_metrics.set_override("moz", None)
    seo_metrics.set_override("ahrefs", None)
    monkeypatch.setattr(config, "MOZ_API_TOKEN", "")
    monkeypatch.setattr(config, "AHREFS_API_KEY", "")
    monkeypatch.setattr(config, "EMAIL_VERIFY_PROVIDER", "none")
    monkeypatch.setattr(config, "OUTREACH_CSV_PATH", tmp_path / "output" / "niche_websites_outreach.csv")
    monkeypatch.setattr(config, "DISCOVERY_MAX_SEARCHES", 2)
    yield fake
    search_providers.set_provider_override(None)
    seo_metrics.set_override("moz", None)
    seo_metrics.set_override("ahrefs", None)
    email_verify.set_override(None)


def crawler():
    return Crawler(client=httpx.Client(transport=httpx.MockTransport(site_handler), follow_redirects=True), delay=0)


def run(trigger="manual", force=True):
    return discovery.run_discovery(trigger, force=force, crawler=crawler())


def by_domain():
    return {w["domain"]: w for w in websites.list_websites()}


# ------------------------------------------------------------------ normalization

@pytest.mark.parametrize("raw,expected", [
    ("https://www.Example.com/path?utm_source=x", "example.com"),
    ("http://blog.example.co.uk/a", "example.co.uk"),
    ("example.com", "example.com"),
    ("WWW.EXAMPLE.COM:443/", "example.com"),
    ("editor@news.example.org", "example.org"),
    ("https://sub.domain.example.com.au/x", "example.com.au"),
    ("", ""),
])
def test_root_domain_normalization(raw, expected):
    assert root_domain(raw) == expected


def test_clean_url_strips_tracking_and_fragments():
    assert clean_url("https://Site.com/post/?utm_source=a&id=3&fbclid=z#top") == "https://site.com/post?id=3"
    assert clean_url("javascript:alert(1)") == ""


# ------------------------------------------------------------------ search providers

def _brave(handler):
    return search_providers.BraveSearch(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_brave_parses_results(monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "brave-test-key")
    seen = {}

    def handler(req):
        seen["token"] = req.headers.get("X-Subscription-Token")
        seen["q"] = req.url.params.get("q")
        return httpx.Response(200, json={"web": {"results": [{"url": "https://a.com/write-for-us", "title": "A", "description": "d"}]}})

    out = _brave(handler).search('education "write for us"')
    assert out == [{"url": "https://a.com/write-for-us", "title": "A", "snippet": "d"}]
    assert seen == {"token": "brave-test-key", "q": 'education "write for us"'}


@pytest.mark.parametrize("status,quota", [(429, True), (401, True), (500, False)])
def test_search_errors_are_reported_not_hidden(monkeypatch, status, quota):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "secret-brave-key-123")
    with pytest.raises(search_providers.SearchError) as exc:
        _brave(lambda req: httpx.Response(status, text="err")).search("q")
    assert exc.value.quota is quota
    assert "secret-brave-key-123" not in str(exc.value)


def test_missing_search_key_is_explained(monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    assert "BRAVE_API_KEY" in search_providers.BraveSearch().config_problem()


def test_search_quota_error_stops_searching_and_is_recorded(disc):
    disc.error = search_providers.SearchError("brave: rate limit", quota=True)
    r = run()
    assert r["status"] == "completed"
    assert len(disc.calls) == 1  # stopped after the quota error
    assert any("rate limit" in e for e in r["errors"])


# ------------------------------------------------------------------ qualification

def test_discovery_qualifies_relevant_sites_and_rejects_bad_ones(disc):
    r = run()
    assert r["status"] == "completed", r["errors"]
    sites = by_domain()

    bio = sites["biosecure-blog.com"]
    assert bio["discovery_status"] == "Qualified"
    assert bio["website_name"] == "BioSecure Blog"
    assert bio["niche"] == "Biometric Devices"
    assert bio["email"] == "editor@biosecure-blog.com"           # editorial address preferred over privacy@
    assert bio["contact_source_url"].endswith("/write-for-us")
    assert bio["contact_name"] == "Maria Lopez"                  # stated next to "Editor:" on the page
    assert bio["guidelines_url"].endswith("/write-for-us")
    assert bio["accepts_guest_posts"] == "Yes"
    assert "fingerprint-scanners" in bio["article_url"] and "utm_" not in bio["article_url"]
    assert bio["article_retrieved"] == 1
    assert bio["email_verification"] == "Not Checked"            # no service configured: never "Verified"
    assert bio["suggested_topic"]
    assert "retrieved" in bio["personalization_notes"]
    assert bio["da"] is None and bio["dr"] is None               # no API keys: left blank, not guessed

    assert sites["cheap-links-hub.net"]["discovery_status"] == "Rejected"       # spam
    assert sites["tasty-recipes-kitchen.com"]["discovery_status"] == "Rejected" # irrelevant
    edu = sites["edu-insights.org"]
    assert edu["discovery_status"] == "Rejected" and edu["accepts_guest_posts"] == "No"
    b2b = sites["b2b-growth-notes.com"]
    assert b2b["discovery_status"] == "Needs Review" and b2b["email"] == ""      # missing email -> review, not invented
    assert "no public contact email" in b2b["review_reason"]
    private = sites["private-tech-site.com"]
    assert private["discovery_status"] == "Needs Review" and "robots.txt" in private["review_reason"]

    assert "medium.com" not in sites and "example-list.com" not in sites
    s = r["summary"]
    assert s["skipped_platforms"] == 2
    assert s["duplicate_results"] >= 1
    assert s["new_websites"] == 6


def test_personalization_notes_differ_between_sites(disc):
    disc.results = RESULTS[:1] + [RESULTS[5]]
    run()
    sites = by_domain()
    notes = {sites[d]["personalization_notes"] for d in ("biosecure-blog.com", "b2b-growth-notes.com")}
    assert len(notes) == 2


def test_rerun_does_not_duplicate_or_recrawl(disc):
    run()
    count = len(by_domain())
    r2 = run()
    assert len(by_domain()) == count
    assert r2["summary"]["new_websites"] == 0
    assert r2["summary"]["existing_websites"] >= 4
    assert r2["usage"]["cached_queries"] >= 1  # earlier search results reused, not re-bought


def test_manual_status_is_not_overwritten_by_automation(disc, monkeypatch):
    run()
    w = by_domain()["b2b-growth-notes.com"]
    websites.set_discovery_status([w["id"]], "Qualified")
    monkeypatch.setattr(config, "DISCOVERY_REVISIT_DAYS", 0)
    run()
    assert by_domain()["b2b-growth-notes.com"]["discovery_status"] == "Qualified"


# ------------------------------------------------------------------ metrics

def test_metrics_store_only_valid_provider_values_and_are_cached(disc):
    moz = FakeMetric("moz", seo_metrics.MOZ_SOURCE, {"biosecure-blog.com": 42, "b2b-growth-notes.com": 150})
    ahrefs = FakeMetric("ahrefs", seo_metrics.AHREFS_FREE_SOURCE, {"biosecure-blog.com": 37.5, "b2b-growth-notes.com": "abc"})
    seo_metrics.set_override("moz", moz)
    seo_metrics.set_override("ahrefs", ahrefs)
    run()
    sites = by_domain()
    assert sites["biosecure-blog.com"]["da"] == 42 and sites["biosecure-blog.com"]["dr"] == 37.5
    assert sites["biosecure-blog.com"]["da_source"] == seo_metrics.MOZ_SOURCE
    assert sites["b2b-growth-notes.com"]["da"] is None and "no value" in sites["b2b-growth-notes.com"]["da_source"]
    assert sites["b2b-growth-notes.com"]["dr"] is None
    assert "cheap-links-hub.net" not in moz.calls[0]  # rejected sites aren't looked up
    run()
    assert len(moz.calls) == 1 and len(ahrefs.calls) == 1  # cached: no repeat requests


def test_moz_and_ahrefs_response_parsing(monkeypatch):
    monkeypatch.setattr(config, "MOZ_API_TOKEN", "moz-token-xyz")
    monkeypatch.setattr(config, "AHREFS_API_KEY", "ahrefs-key-xyz")
    monkeypatch.setattr(config, "AHREFS_DR_ENDPOINT", "free")

    def moz_handler(req):
        body = json.loads(req.content)
        assert req.headers["x-moz-token"] == "moz-token-xyz"
        assert body["method"] == "data.site.metrics.fetch"
        q = body["params"]["data"]["site_query"]["query"]
        if q == "bad.com":
            return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"message": "not found"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"site_metrics": {"domain_authority": 55}}})

    moz = seo_metrics.MozDA(client=httpx.Client(transport=httpx.MockTransport(moz_handler)))
    assert moz.fetch(["good.com", "bad.com"]) == {"good.com": 55.0, "bad.com": None}

    def ahrefs_handler(req):
        assert req.headers["authorization"] == "Bearer ahrefs-key-xyz"
        assert json.loads(req.content)["targets"] == ["good.com", "other.com"]
        return httpx.Response(200, json={"domain_rating": {"license": "x", "targets": [
            {"target": "good.com", "domain_rating": 61.0}, {"target": "other.com", "domain_rating": None}]}})

    ahrefs = seo_metrics.AhrefsDR(client=httpx.Client(transport=httpx.MockTransport(ahrefs_handler)))
    assert ahrefs.fetch(["good.com", "other.com"]) == {"good.com": 61.0, "other.com": None}

    quota = seo_metrics.AhrefsDR(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    with pytest.raises(seo_metrics.MetricError) as exc:
        quota.fetch(["good.com"])
    assert exc.value.quota


def test_missing_metric_keys_are_explained_not_faked(disc):
    r = run()
    assert any(n.startswith("DA: Not Checked") for n in r["notices"])
    assert any(n.startswith("DR: Not Checked") for n in r["notices"])
    assert not any("DA:" in e or "DR:" in e for e in r["errors"])  # an optional service being absent isn't an error
    rows = list(csv.DictReader(open(config.OUTREACH_CSV_PATH, encoding="utf-8-sig")))
    bio = next(x for x in rows if "biosecure" in x["Website URL"])
    assert bio["DA"] == "Not Checked" and bio["DR"] == "Not Checked" and bio["DA Source"] == "Not Checked"


# ------------------------------------------------------------------ email verification

@pytest.mark.parametrize("raw,expected", [("valid", "Verified"), ("accept_all", "Catch-All"), ("invalid", "Invalid"),
                                          ("unknown", "Unknown"), ("webmail", "Unknown")])
def test_hunter_status_mapping(monkeypatch, raw, expected):
    monkeypatch.setattr(config, "HUNTER_API_KEY", "hunter-key")
    v = email_verify.HunterVerifier(client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": {"status": raw}}))))
    assert v.verify("editor@site.com")[0] == expected


@pytest.mark.parametrize("raw,expected", [("valid", "Verified"), ("catch-all", "Catch-All"), ("do_not_mail", "Invalid")])
def test_zerobounce_status_mapping(monkeypatch, raw, expected):
    monkeypatch.setattr(config, "ZEROBOUNCE_API_KEY", "zb-key")
    v = email_verify.ZeroBounceVerifier(client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"status": raw}))))
    assert v.verify("editor@site.com")[0] == expected


def test_syntax_only_is_never_verified():
    assert email_verify.local_status("editor@site.com") == ("Not Checked", "format OK; mailbox not verified")
    assert email_verify.local_status("not-an-email")[0] == "Unknown"
    assert email_verify.local_status("")[0] == "Unknown"


def test_verification_service_results_are_used_and_cached(disc):
    class Fake:
        name = "fakeverify"
        calls = []

        def verify(self, email):
            self.calls.append(email)
            return ("Catch-All", "accept_all")

    fake = Fake()
    email_verify.set_override(fake)
    run()
    assert by_domain()["biosecure-blog.com"]["email_verification"] == "Catch-All"
    n = len(fake.calls)
    run()
    assert len(fake.calls) == n


# ------------------------------------------------------------------ CSV

def test_csv_has_exact_column_order_and_escapes_text(disc):
    run()
    assert csv_sync.read_header() == EXPECTED_HEADER
    with db.transaction() as conn:
        conn.execute("UPDATE websites SET personalization_notes=?, website_name=? WHERE domain='biosecure-blog.com'",
                     ('Says "hello", then\nnew line, café', "=HYPERLINK(\"x\")"))
    csv_sync.export()
    rows = list(csv.DictReader(open(config.OUTREACH_CSV_PATH, encoding="utf-8-sig", newline="")))
    bio = next(r for r in rows if "biosecure" in r["Website URL"])
    assert bio["Personalization Notes"] == 'Says "hello", then\nnew line, café'
    assert bio["Website Name"].startswith("'=")  # formula injection neutralised
    assert all(len(r) == len(EXPECTED_HEADER) for r in rows)


def test_csv_merges_instead_of_overwriting(disc):
    run()
    path = config.OUTREACH_CSV_PATH
    rows = list(csv.reader(open(path, encoding="utf-8-sig", newline="")))
    hand = ["Hand Added Site", "https://hand-added.com", "Sam", "sam@hand-added.com", "Technology", "", "", "my note",
            "New", "Unknown", "99", "99", "", "", "", "", "", "", "Qualified"]
    for r in rows[1:]:
        if "b2b-growth-notes" in r[1]:
            r[2] = "Pat Kim"  # filled a blank contact name in Excel
    rows.append(hand)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerows(rows)

    run()  # discovery re-exports
    sites = by_domain()
    assert sites["hand-added.com"]["email"] == "sam@hand-added.com"
    assert sites["hand-added.com"]["da"] is None  # DA typed in the CSV is not trusted
    assert sites["b2b-growth-notes.com"]["contact_name"] == "Pat Kim"
    assert path.with_name("niche_websites_outreach.backup.csv").exists()
    text = path.read_text(encoding="utf-8-sig")
    assert "hand-added.com" in text and "biosecure-blog.com" in text


def test_exported_csv_can_be_imported_as_contacts(client, disc):
    run()
    content = config.OUTREACH_CSV_PATH.read_bytes()
    r = client.post("/api/import", files={"file": ("niche_websites_outreach.csv", content, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] >= 2  # rows with emails


# ------------------------------------------------------------------ daily scheduling and recovery

def test_runs_once_per_day_unless_forced(disc):
    first = run(trigger="startup", force=False)
    assert first["started"] and first["status"] == "completed"
    second = run(trigger="startup", force=False)
    assert second["started"] is False and "already completed today" in second["message"]
    manual = run(trigger="manual", force=True)
    assert manual["started"]


def test_failed_run_does_not_count_as_done_today(disc, monkeypatch):
    real = discovery.collect_candidates
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(discovery, "collect_candidates", flaky)
    r = run(force=False)
    assert r["status"] == "failed" and any("boom" in e for e in r["errors"])
    again = run(force=False)
    assert again["started"] and again["status"] == "completed"


def test_interrupted_run_is_recovered_and_data_kept(disc):
    run()
    kept = len(by_domain())
    with db.transaction() as conn:
        conn.execute("INSERT INTO discovery_runs(run_date, trigger, status, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
                     (db.local_today(), "scheduled", "running", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
    s = discovery.status()
    assert s["running"] is None
    assert s["runs"][0]["status"] == "interrupted"
    assert run()["started"]
    assert len(by_domain()) == kept


def test_concurrent_run_is_refused(disc):
    with db.transaction() as conn:
        conn.execute("INSERT INTO discovery_runs(run_date, trigger, status, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
                     (db.local_today(), "cli", "running", db.now(), db.now()))
    r = run()
    assert r["started"] is False and "already in progress" in r["message"]


def test_startup_marks_dashboard_runs_from_dead_process_interrupted(disc, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ON_STARTUP", False)
    with db.transaction() as conn:
        conn.execute("INSERT INTO discovery_runs(run_date, trigger, status, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
                     (db.local_today(), "manual", "running", db.now(), db.now()))
    assert discovery.on_startup()["recovered"] == 1


# ------------------------------------------------------------------ outreach safety & history

def test_discovery_never_sends_or_drafts(client, disc, provider):
    run()
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 0
    assert provider.sent == []


def test_added_contacts_start_unverified_and_cannot_be_sent(client, disc, provider):
    run()
    bio = by_domain()["biosecure-blog.com"]
    r = client.post("/api/discovery/websites/to-contacts", json={"ids": [bio["id"]]}).json()
    assert len(r["created"]) == 1
    c = client.get("/api/contacts").json()[0]
    assert c["email"] == "editor@biosecure-blog.com" and c["email_verified"] == 0
    assert "Contact source:" in c["notes"]
    d = client.post("/api/drafts/generate", json={"ids": [c["id"]]}).json()
    assert d["created"]
    approve(client, d["created"])
    r = client.post("/api/send/prepare", json={"ids": d["created"]}).json()
    assert r["batch"] is None
    assert "Email source is not marked as verified" in r["excluded"][0]["reasons"]
    assert provider.sent == []
    assert by_domain()["biosecure-blog.com"]["outreach_label"] == "Approved"


def test_rejected_and_emailless_websites_cannot_become_contacts(client, disc):
    run()
    s = by_domain()
    r = client.post("/api/discovery/websites/to-contacts",
                    json={"ids": [s["cheap-links-hub.net"]["id"], s["b2b-growth-notes.com"]["id"]]}).json()
    assert r["created"] == [] and len(r["skipped"]) == 2


def test_existing_outreach_history_is_preserved(client, disc, provider):
    import_csv(client, ["BioSecure,https://biosecure-blog.com,Maria,maria@biosecure-blog.com,Biometrics,,,my own note,new,yes"])
    cid = client.get("/api/contacts").json()[0]["id"]
    ids = client.post("/api/drafts/generate", json={"ids": [cid]}).json()["created"]
    approve(client, ids)
    prepare_and_confirm(client, ids)
    assert len(provider.sent) == 1
    run()
    contact = client.get("/api/contacts").json()[0]
    assert contact["outreach_status"] == "contacted" and contact["notes"] == "my own note"
    w = by_domain()["biosecure-blog.com"]
    assert w["contact_id"] == cid and w["outreach_label"] == "Sent"
    assert w["email"] == "maria@biosecure-blog.com"  # your contact wasn't replaced by the scraped one
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM websites WHERE domain='biosecure-blog.com'").fetchone()[0] == 1
        assert conn.execute("SELECT send_status FROM drafts WHERE id=?", (ids[0],)).fetchone()[0] == "sent"


def test_same_website_cannot_be_pitched_twice_via_another_address(client, provider):
    import_csv(client, ["Site,https://samesite.com,A,a@samesite.com,Technology,,,,new,yes",
                        "Site,https://www.samesite.com/blog,B,b@samesite.com,Technology,,,,new,yes"])
    cs = sorted(client.get("/api/contacts").json(), key=lambda c: c["email"])
    first = client.post("/api/drafts/generate", json={"ids": [cs[0]["id"]]}).json()["created"]
    approve(client, first)
    prepare_and_confirm(client, first)
    r = client.post("/api/drafts/generate", json={"ids": [cs[1]["id"]]}).json()
    assert r["created"] == []
    assert "website was already contacted" in r["skipped"][0]["reason"]


def test_do_not_contact_from_discovery_suppresses_email(client, disc):
    run()
    bio = by_domain()["biosecure-blog.com"]
    client.put(f"/api/discovery/websites/{bio['id']}", json={"outreach_status": "Do Not Contact"})
    assert any(s["email_norm"] == "editor@biosecure-blog.com" for s in client.get("/api/suppression").json())
    r = client.post("/api/discovery/websites/to-contacts", json={"ids": [bio["id"]]}).json()
    assert r["created"] == []


# ------------------------------------------------------------------ API & dashboard

def test_dashboard_api_filters_sorting_and_export(client, disc):
    seo_metrics.set_override("moz", FakeMetric("moz", seo_metrics.MOZ_SOURCE, {"biosecure-blog.com": 40, "b2b-growth-notes.com": 20}))
    run()
    status = client.get("/api/discovery/status").json()
    assert status["stats"]["qualified"] == 1 and status["stats"]["missing_email"] >= 1
    assert status["last_completed"]["summary"]["new_websites"] == 6
    assert client.get("/api/discovery/websites", params={"niche": "Biometric Devices"}).json()[0]["domain"] == "biosecure-blog.com"
    assert [w["domain"] for w in client.get("/api/discovery/websites", params={"da_min": 30}).json()] == ["biosecure-blog.com"]
    by_da = client.get("/api/discovery/websites", params={"sort": "da", "direction": "desc"}).json()
    assert by_da[0]["domain"] == "biosecure-blog.com" and by_da[1]["domain"] == "b2b-growth-notes.com"
    assert client.get("/api/discovery/websites", params={"discovery_status": "Rejected"}).json()
    assert client.get("/api/discovery/websites", params={"da_min": "abc"}).status_code == 400
    r = client.get("/api/discovery/export.csv", params={"scope": "filtered", "niche": "Biometric Devices"})
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == EXPECTED_HEADER and len(rows) == 2


def test_manual_run_endpoint_starts_background_run(client, disc, monkeypatch):
    started = []
    monkeypatch.setattr(discovery, "run_discovery", lambda trigger, force: started.append((trigger, force)))
    r = client.post("/api/discovery/run").json()
    assert r["started"]
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.05)
    assert started == [("manual", True)]


def test_secrets_never_reach_run_log_or_csv(disc, monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "TOPSECRETKEY987")
    disc.error = search_providers.SearchError("failed url https://x/?key=TOPSECRETKEY987", quota=True)
    run()
    with db.transaction() as conn:
        dump = json.dumps(db.rows(conn, "SELECT errors, api_usage, summary FROM discovery_runs"))
    assert "TOPSECRETKEY987" not in dump
    assert "TOPSECRETKEY987" not in config.OUTREACH_CSV_PATH.read_text(encoding="utf-8-sig")
