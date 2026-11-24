from app import db
from app.topics import detect_niche, focus_from_url, suggest_topics
from tests.conftest import approve, contact_row, import_csv, setup_drafts


def test_topics_per_niche():
    assert detect_niche("EdTech") == "edtech"
    assert detect_niche("Software and Digital Transformation") == "software"
    assert detect_niche("B2B business services") == "business_services"
    assert focus_from_url("https://x.com/category/online-learning/") == "online learning"
    assert focus_from_url("https://x.com/") == ""
    for niche in ["Education", "EdTech", "Technology", "Business services", "Business growth", "Software"]:
        topics = suggest_topics({"email": "a@b.com", "niche": niche, "article_url": ""})
        assert len(topics) == 3 and len(set(topics)) == 3
        assert all("{focus}" not in t for t in topics)
    with_focus = suggest_topics({"email": "a@b.com", "niche": "Education", "article_url": "https://x.com/category/stem-education"})
    assert any("STEM Education" in t for t in with_focus)


def test_draft_generation_content(client, provider):
    ids = setup_drafts(client, 2)
    assert len(ids) == 2
    d = client.get(f"/api/drafts/{ids[0]}").json()
    assert d["approval_status"] == "pending" and d["send_status"] == "not_sent"
    body = d["body"]
    assert body.startswith("Hi Editor,")
    assert "Test Sender" in body
    assert "guidelines" in body or "guest" in body.lower()
    assert "won't contact you again" in body          # opt-out line
    assert d["subject"]
    # Generating drafts never sends anything.
    assert provider.sent == []
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 0


def test_generic_greeting_without_name(client):
    import_csv(client, ["Biz Blog,https://biz.com,,info@biz.com,Business growth,,,,new,yes"])
    cid = client.get("/api/contacts").json()[0]["id"]
    did = client.post("/api/drafts/generate", json={"ids": [cid]}).json()["created"][0]
    assert client.get(f"/api/drafts/{did}").json()["body"].startswith("Hello Biz Blog team,")


def test_greeting_names():
    from app.templates import first_name
    assert first_name("Jane Doe") == "Jane"
    assert first_name("Dr. Maria Lopez") == "Dr. Lopez"
    assert first_name("") == ""
    assert first_name("info@site.com") == ""


def test_generation_limit_and_skips(client):
    r = client.post("/api/drafts/generate", json={"ids": list(range(1, 32))})
    assert r.status_code == 400
    import_csv(client, [contact_row(1, email="bad-email")])
    cid = client.get("/api/contacts").json()[0]["id"]
    r = client.post("/api/drafts/generate", json={"ids": [cid]}).json()
    assert r["created"] == [] and "Invalid email" in r["skipped"][0]["reason"]


def test_no_duplicate_drafts_for_same_contact(client):
    setup_drafts(client, 1)
    cid = client.get("/api/contacts").json()[0]["id"]
    r = client.post("/api/drafts/generate", json={"ids": [cid]}).json()
    assert r["created"] == [] and "already exists" in r["skipped"][0]["reason"]


def test_approval_recorded_in_database(client):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    with db.transaction() as conn:
        d = db.row(conn, "SELECT * FROM drafts WHERE id=?", (did,))
        log = db.rows(conn, "SELECT action FROM approval_log WHERE draft_id=?", (did,))
    assert d["approval_status"] == "approved"
    assert d["approved_hash"] == d["content_hash"]
    assert d["approved_at"]
    assert "approved" in [l["action"] for l in log]


def test_rejection(client, provider):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    client.post("/api/drafts/reject", json={"ids": [did]})
    d = client.get(f"/api/drafts/{did}").json()
    assert d["approval_status"] == "rejected"
    r = client.post("/api/send/prepare", json={"ids": [did]}).json()
    assert r["batch"] is None and "not approved" in r["excluded"][0]["reasons"][0]
    assert provider.sent == []


def test_editing_approved_draft_requires_reapproval(client, provider):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    r = client.put(f"/api/drafts/{did}", json={"subject": "A different subject"})
    assert r.status_code == 200
    assert r.json()["approval_status"] == "pending"
    d = client.get(f"/api/drafts/{did}").json()
    assert d["draft_status"] == "edited" and not d["approval_valid"]
    assert "approval_revoked_by_edit" in [h["action"] for h in d["history"]]
    # Cannot be sent until approved again.
    r = client.post("/api/send/prepare", json={"ids": [did]}).json()
    assert r["batch"] is None
    approve(client, [did])
    assert client.post("/api/send/prepare", json={"ids": [did]}).json()["batch"] is not None


def test_saving_without_changes_keeps_approval(client):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    d = client.get(f"/api/drafts/{did}").json()
    r = client.put(f"/api/drafts/{did}", json={"subject": d["subject"], "body": d["body"]})
    assert r.json()["approval_status"] == "approved"


def test_delete_draft(client):
    [did] = setup_drafts(client, 1)
    assert client.delete(f"/api/drafts/{did}").status_code == 200
    assert client.get(f"/api/drafts/{did}").status_code == 404
