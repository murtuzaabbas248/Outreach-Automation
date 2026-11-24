from app import db
from tests.conftest import approve, contact_row, import_csv, prepare_and_confirm, set_settings, setup_drafts


def contact_id(client, email):
    return next(c["id"] for c in client.get("/api/contacts").json() if c["email"] == email)


def test_suppression_blocks_drafts_and_sending(client, provider):
    ids = setup_drafts(client, 2)
    approve(client, ids)
    r = client.post("/api/suppression", json={"email": "Editor0@site0.com"}).json()
    assert r["drafts_cancelled"] == 1
    prep = client.post("/api/send/prepare", json={"ids": ids}).json()
    assert [i["to_email"] for i in prep["items"]] == ["editor1@site1.com"]
    suppressed_draft = next(i for i in ids if client.get(f"/api/drafts/{i}").json()["to_email"] == "editor0@site0.com")
    assert [x["draft_id"] for x in prep["excluded"]] == [suppressed_draft]


def test_suppressed_address_flagged_on_import(client):
    client.post("/api/suppression", json={"email": "editor5@site5.com"})
    report = import_csv(client, [contact_row(5)])
    assert len(report["suppressed"]) == 1
    cid = contact_id(client, "editor5@site5.com")
    r = client.post("/api/drafts/generate", json={"ids": [cid]}).json()
    assert r["created"] == []


def test_opt_out_adds_suppression_and_cancels_drafts(client, provider):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    cid = contact_id(client, "editor0@site0.com")
    r = client.post(f"/api/contacts/{cid}/status", json={"status": "opted_out"}).json()
    assert r["drafts_cancelled"] == 1
    assert [s["email_norm"] for s in client.get("/api/suppression").json()] == ["editor0@site0.com"]
    assert client.post("/api/send/prepare", json={"ids": [did]}).json()["batch"] is None
    assert provider.sent == []


def test_remove_contact_removes_pending_drafts(client):
    [did] = setup_drafts(client, 1)
    cid = contact_id(client, "editor0@site0.com")
    r = client.delete(f"/api/contacts/{cid}").json()
    assert r["drafts_removed"] == 1
    assert client.get(f"/api/drafts/{did}").status_code == 404
    assert client.get("/api/contacts").json() == []


def _send_one(client):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    prepare_and_confirm(client, [did])
    return did


def test_followups_disabled_by_default(client, provider):
    did = _send_one(client)
    r = client.post("/api/followups/generate", json={"ids": [did], "delay_days": 3})
    assert r.status_code == 400 and "disabled" in r.json()["detail"]


def test_followup_requires_approval_and_due_date(client, provider):
    did = _send_one(client)
    set_settings(followups_enabled="true")
    r = client.post("/api/followups/generate", json={"ids": [did], "delay_days": 3}).json()
    [fid] = r["created"]
    f = client.get(f"/api/drafts/{fid}").json()
    assert f["kind"] == "followup" and f["approval_status"] == "pending"
    assert f["subject"].startswith("Following up:")

    # Unapproved follow-up cannot be sent.
    assert client.post("/api/send/prepare", json={"ids": [fid]}).json()["batch"] is None
    approve(client, [fid])
    # Approved but not yet due.
    prep = client.post("/api/send/prepare", json={"ids": [fid]}).json()
    assert prep["batch"] is None and any("not due" in x for x in prep["excluded"][0]["reasons"])
    # Make it due, then it can be sent.
    with db.transaction() as conn:
        conn.execute("UPDATE drafts SET due_at='2000-01-01T00:00:00+00:00' WHERE id=?", (fid,))
    prepare_and_confirm(client, [fid])
    assert len(provider.sent) == 2

    # Never more than one follow-up per email.
    r = client.post("/api/followups/generate", json={"ids": [did], "delay_days": 3}).json()
    assert r["created"] == [] and "already exists" in r["skipped"][0]["reason"]


def test_reply_cancels_pending_followup(client, provider):
    did = _send_one(client)
    set_settings(followups_enabled="true")
    [fid] = client.post("/api/followups/generate", json={"ids": [did], "delay_days": 3}).json()["created"]
    approve(client, [fid])
    cid = contact_id(client, "editor0@site0.com")
    r = client.post(f"/api/contacts/{cid}/status", json={"status": "replied"}).json()
    assert r["drafts_cancelled"] == 1
    f = client.get(f"/api/drafts/{fid}").json()
    assert f["draft_status"] == "cancelled" and f["approval_status"] == "pending"
    with db.transaction() as conn:
        conn.execute("UPDATE drafts SET due_at='2000-01-01T00:00:00+00:00' WHERE id=?", (fid,))
    assert client.post("/api/send/prepare", json={"ids": [fid]}).json()["batch"] is None
    assert len(provider.sent) == 1


def test_do_not_contact_cancels_followup(client, provider):
    did = _send_one(client)
    set_settings(followups_enabled="true")
    [fid] = client.post("/api/followups/generate", json={"ids": [did], "delay_days": 3}).json()["created"]
    client.post("/api/suppression", json={"email": "editor0@site0.com"})
    assert client.get(f"/api/drafts/{fid}").json()["draft_status"] == "cancelled"


def test_disabling_followups_cancels_pending(client, provider):
    did = _send_one(client)
    client.put("/api/settings", json={"followups_enabled": True})
    [fid] = client.post("/api/followups/generate", json={"ids": [did], "delay_days": 3}).json()["created"]
    client.put("/api/settings", json={"followups_enabled": False})
    assert client.get(f"/api/drafts/{fid}").json()["draft_status"] == "cancelled"


def test_settings_validation(client):
    assert client.put("/api/settings", json={"send_delay_seconds": 1}).status_code == 400
    assert client.put("/api/settings", json={"provider": "spammer"}).status_code == 400
    assert client.put("/api/settings", json={"test_recipient": "nope"}).status_code == 400
    assert client.put("/api/settings", json={"send_delay_seconds": 30}).status_code == 200
