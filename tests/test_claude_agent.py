"""Claude auto-discovery: the app runs `claude -p`, parses its JSON, imports, inspects and stores DR.
Claude itself is faked here; nothing calls Claude, searches the web, or sends email."""
import json

import pytest

from app import claude_agent, config, db, discovery, free_discovery, websites

from .test_discovery import crawler


def reply(payload, **outer):
    return json.dumps({"type": "result", "is_error": False, "num_turns": 9, "duration_ms": 240000,
                       "total_cost_usd": 1.5, "result": "Here you go:\n" + json.dumps(payload), **outer})


PAYLOAD = {
    "sites": [
        {"url": "https://biosecure-blog.com/write-for-us", "niche": "biometrics", "query": "biometrics write for us"},
        {"url": "https://www.biosecure-blog.com/", "niche": "biometrics", "query": "biometrics write for us"},  # same site
        {"url": "https://b2b-growth-notes.com/contribute", "niche": "business_services", "query": "b2b contribute"},
        {"url": "https://medium.com/x/write-for-us", "niche": "technology", "query": "tech"},        # platform: skipped
        {"url": "https://recipes.example/write", "niche": "cooking", "query": "food"},               # not a niche asked for
        {"url": "javascript:alert(1)", "niche": "education", "query": "x"},                          # not a URL
    ],
    "dr": {"biosecure-blog.com": 48, "b2b-growth-notes.com": "high", "never-seen.com": 30, "bad.com": 300},
    "queries": ["biometrics write for us", "b2b contribute"],
    "notes": "Found a few.",
}


@pytest.fixture
def fake_claude(client, tmp_path, monkeypatch):
    from app import search_providers, seo_metrics
    search_providers.set_provider_override(None)
    seo_metrics.set_override("moz", None)
    seo_metrics.set_override("ahrefs", None)
    for k, v in {"SEARCH_PROVIDER": "manual", "MOZ_API_TOKEN": "", "AHREFS_API_KEY": "", "EMAIL_VERIFY_PROVIDER": "none"}.items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(config, "OUTREACH_CSV_PATH", tmp_path / "out" / "niche_websites_outreach.csv")
    real_run = discovery.run_discovery
    monkeypatch.setattr(discovery, "run_discovery", lambda trigger, force=True: real_run(trigger, force, crawler=crawler()))
    prompts = []
    state = {"stdout": reply(PAYLOAD)}

    def runner(prompt):
        prompts.append(prompt)
        if isinstance(state["stdout"], Exception):
            raise state["stdout"]
        return state["stdout"]

    monkeypatch.setattr(claude_agent, "_runner", runner)
    yield state, prompts


def by_domain():
    return {w["domain"]: w for w in websites.list_websites()}


def test_claude_round_imports_inspects_and_stores_dr(fake_claude):
    state, prompts = fake_claude
    r = claude_agent.run("button", ["biometrics", "business_services", "technology"])
    assert r["status"] == "completed", r["message"]
    s = by_domain()
    bio = s["biosecure-blog.com"]
    assert bio["discovery_status"] == "Qualified" and bio["email"] == "editor@biosecure-blog.com"  # inspected by the app's crawler
    assert bio["dr"] == 48 and "Claude auto-discovery" in bio["dr_source"]
    assert bio["discovery_query"] == "Claude auto-discovery: biometrics write for us"
    assert s["b2b-growth-notes.com"]["dr"] is None   # "high" is not a number: stays Not Checked
    assert "medium.com" not in s and "recipes.example" not in s and "never-seen.com" not in s
    assert bio["da"] is None and bio["email_verification"] == "Not Checked"
    assert r["queued"] == 2 and r["dr_values"] == 1 and r["discovery_run_id"]
    assert json.loads(r["queries"]) == ["biometrics write for us", "b2b contribute"]
    assert "biometrics (Biometric Devices)" in prompts[0]


def test_second_round_skips_known_sites_and_old_queries(fake_claude):
    state, prompts = fake_claude
    claude_agent.run("button", ["biometrics", "business_services"])
    r = claude_agent.run("button", ["biometrics", "business_services"])
    assert r["queued"] == 0 and r["already_known"] == 2
    assert "biosecure-blog.com" in prompts[1] and "biometrics write for us" in prompts[1]  # told to skip them
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM websites WHERE domain='biosecure-blog.com'").fetchone()[0] == 1


@pytest.mark.parametrize("stdout,needle", [
    ("not json", "not JSON"),
    (json.dumps({"is_error": True, "result": "usage limit reached"}), "usage limit"),
    (json.dumps({"is_error": False, "result": "Sorry, no results."}), "did not contain"),
])
def test_bad_claude_output_is_recorded_not_crashing(fake_claude, stdout, needle):
    state, _ = fake_claude
    state["stdout"] = stdout
    r = claude_agent.run("button", ["education"])
    assert r["status"] == "failed" and needle in r["message"]
    assert websites.list_websites() == []


def test_claude_round_never_drafts_or_sends(fake_claude, provider):
    claude_agent.run("button", ["biometrics", "business_services"])
    with db.transaction() as conn:
        for t in ("drafts", "contacts", "send_log"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert provider.sent == []


def test_cli_is_restricted_to_search_and_free_dr():
    cmd = claude_agent.command("claude")
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert allowed == ["WebSearch", "mcp__claude_ai_Ahrefs__public-domain-rating-free"]
    for tool in ("Bash", "PowerShell", "Write", "Edit", "WebFetch", "mcp__claude_ai_Gmail"):
        assert tool in denied
    assert "-p" in cmd and "json" in cmd


def test_api_start_status_daily_and_single_run(fake_claude, client, monkeypatch):
    started = []
    monkeypatch.setattr(claude_agent, "run", lambda trigger, niches, per_niche, run_id=None: started.append(niches))
    r = client.post("/api/claude/discover", json={"niches": ["education", "bogus"]}).json()
    assert r["started"]
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.05)
    assert started == [["education"]]
    # the fake run never finished the row, so a second click is refused
    assert client.post("/api/claude/discover", json={}).json()["started"] is False
    assert client.put("/api/claude/settings", json={"daily": True}).json()["daily"] is True
    st = client.get("/api/claude/status").json()
    assert st["available"] and st["daily"] and st["running"]


def test_daily_option_runs_once_per_day(fake_claude, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_agent, "start_in_background", lambda trigger, *a, **k: calls.append(trigger) or {"started": True, "message": "ok"})
    assert claude_agent.on_startup()["started"] is False  # off by default
    claude_agent.set_daily(True)
    assert claude_agent.on_startup()["started"] and calls == ["daily"]
    claude_agent.run("daily", ["biometrics"])
    assert claude_agent.on_startup()["started"] is False  # already ran today


def test_missing_claude_cli_is_explained(client, monkeypatch):
    monkeypatch.setattr(claude_agent, "_runner", None)
    monkeypatch.setattr(claude_agent, "cli_path", lambda: "")
    assert client.post("/api/claude/discover", json={}).json()["started"] is False
    assert client.get("/api/claude/status").json()["available"] is False
