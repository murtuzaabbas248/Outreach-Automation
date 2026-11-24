"""Tests that prove emails cannot be sent without explicit approval and confirmation."""
from app import db, providers, sender
from tests.conftest import approve, prepare_and_confirm, set_settings, setup_drafts


def sent_to(provider):
    return [m["To"] for m in provider.sent]


# ------------------------------------------------------- unauthorized sending

def test_unapproved_drafts_cannot_be_sent_via_direct_api_calls(client, provider):
    """Bypass the dashboard completely and call the backend directly."""
    ids = setup_drafts(client, 3)

    # 1. prepare with unapproved drafts: nothing becomes sendable
    r = client.post("/api/send/prepare", json={"ids": ids}).json()
    assert r["batch"] is None
    assert len(r["excluded"]) == 3

    # 2. confirm a batch id that doesn't exist / with a guessed token
    r = client.post("/api/send/confirm", json={"batch_id": 1, "token": "guess", "confirm_text": "SEND"})
    assert r.status_code == 400

    # 3. tamper with the DB: mark as approved without going through approve()
    with db.transaction() as conn:
        conn.execute("UPDATE drafts SET approval_status='approved' WHERE id=?", (ids[0],))
    r = client.post("/api/send/prepare", json={"ids": [ids[0]]}).json()
    assert r["batch"] is None
    assert any("changed after approval" in x for x in r["excluded"][0]["reasons"])

    # 4. inject a send_item directly and run the worker: the final gate refuses
    with db.transaction() as conn:
        bid = conn.execute("INSERT INTO send_batches(status, token, mode, provider, created_at) VALUES "
                           "('sending','x','live','gmail',?)", (db.now(),)).lastrowid
        for did in ids:
            conn.execute("INSERT INTO send_items(batch_id, draft_id, approved_hash) VALUES (?,?,?)", (bid, did, "forged"))
    sender.run_batch(bid)

    # 5. call the provider-facing worker function for an unconfirmed batch
    approve(client, [ids[1]])
    prep = client.post("/api/send/prepare", json={"ids": [ids[1]]}).json()
    sender.run_batch(prep["batch"]["id"])  # status is awaiting_confirmation -> no-op

    assert provider.sent == []
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 0
        statuses = {r["status"] for r in conn.execute("SELECT status FROM send_items WHERE batch_id=?", (bid,))}
    assert statuses == {"skipped"}


def test_confirmation_requires_token_and_typed_send(client, provider):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    b = client.post("/api/send/prepare", json={"ids": [did]}).json()["batch"]
    assert b["status"] == "awaiting_confirmation"
    assert provider.sent == []  # preparing never sends

    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "yes"})
    assert r.status_code == 400
    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": "wrong", "confirm_text": "SEND"})
    assert r.status_code == 400
    assert provider.sent == []

    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "SEND"})
    assert r.status_code == 200
    assert sent_to(provider) == ["editor0@site0.com"]
    # Token is single-use.
    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "SEND"})
    assert r.status_code == 400
    assert len(provider.sent) == 1


def test_edit_between_prepare_and_confirm_cancels_batch(client, provider):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    b = client.post("/api/send/prepare", json={"ids": [did]}).json()["batch"]
    client.put(f"/api/drafts/{did}", json={"body": "Completely different body"})
    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "SEND"})
    assert r.status_code == 400
    assert provider.sent == []


def test_rejected_after_queueing_is_not_sent(client, provider):
    ids = setup_drafts(client, 2)
    approve(client, ids)
    b = client.post("/api/send/prepare", json={"ids": ids}).json()["batch"]
    client.post("/api/drafts/reject", json={"ids": [ids[0]]})
    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "SEND"})
    assert r.status_code == 400  # batch changed after preparation -> cancelled
    assert provider.sent == []


def test_unverified_email_source_blocks_sending(client, provider):
    [did] = setup_drafts(client, 1, verified="no")
    approve(client, [did])
    r = client.post("/api/send/prepare", json={"ids": [did]}).json()
    assert r["batch"] is None
    assert any("verified" in x for x in r["excluded"][0]["reasons"])


def test_cancel_before_sending(client, provider):
    ids = setup_drafts(client, 2)
    approve(client, ids)
    b = client.post("/api/send/prepare", json={"ids": ids}).json()["batch"]
    assert client.post(f"/api/send/batches/{b['id']}/cancel").status_code == 200
    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "SEND"})
    assert r.status_code == 400
    assert provider.sent == []


def test_cancel_mid_batch_stops_remaining(client, provider):
    ids = setup_drafts(client, 3)
    approve(client, ids)

    class CancelAfterFirst:
        def __init__(self):
            self.sent = []

        def check_ready(self):
            pass

        def send(self, msg):
            self.sent.append(msg)
            with db.transaction() as conn:
                bid = conn.execute("SELECT id FROM send_batches WHERE status='sending'").fetchone()[0]
            sender.cancel_batch(bid)
            return "ok"

    p = CancelAfterFirst()
    providers.set_provider_override(p)
    bid = prepare_and_confirm(client, ids)
    assert len(p.sent) == 1
    items = client.get(f"/api/send/batches/{bid}").json()["items"]
    assert sorted(i["status"] for i in items) == ["cancelled", "cancelled", "sent"]


# ------------------------------------------------------- limits

def test_batch_size_limit(client):
    r = client.post("/api/send/prepare", json={"ids": list(range(1, 32))})
    assert r.status_code == 400 and "at most 30" in r.json()["detail"]


def test_daily_limit(client, provider):
    set_settings(daily_send_limit="2")
    ids = setup_drafts(client, 3)
    approve(client, ids)
    r = client.post("/api/send/prepare", json={"ids": ids})
    assert r.status_code == 400 and "Daily limit" in r.json()["detail"]
    prepare_and_confirm(client, ids[:2])
    assert len(provider.sent) == 2
    r = client.post("/api/send/prepare", json={"ids": [ids[2]]})
    assert r.status_code == 400
    assert len(provider.sent) == 2


def test_messages_sent_individually(client, provider):
    ids = setup_drafts(client, 3)
    approve(client, ids)
    prepare_and_confirm(client, ids)
    assert len(provider.sent) == 3
    for m in provider.sent:
        assert "," not in m["To"] and m["Cc"] is None and m["Bcc"] is None
    assert len(set(sent_to(provider))) == 3


# ------------------------------------------------------- duplicates

def test_duplicate_prevention_across_batches(client, provider):
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    prepare_and_confirm(client, [did])
    assert len(provider.sent) == 1
    # Already sent: cannot be re-queued.
    r = client.post("/api/send/prepare", json={"ids": [did]}).json()
    assert r["batch"] is None
    # A new contact row with the same address can't get a new draft either.
    cid = client.get("/api/contacts").json()[0]["id"]
    with db.transaction() as conn:
        conn.execute("UPDATE contacts SET outreach_status='new' WHERE id=?", (cid,))
    r = client.post("/api/drafts/generate", json={"ids": [cid]}).json()
    assert r["created"] == [] and "already contacted" in r["skipped"][0]["reason"]
    assert len(provider.sent) == 1


# ------------------------------------------------------- failures

def test_definite_failure_is_recorded(client, provider):
    ids = setup_drafts(client, 2)
    approve(client, ids)
    provider.script = [providers.SendRejected("Invalid To header"), None]
    bid = prepare_and_confirm(client, ids)
    items = client.get(f"/api/send/batches/{bid}").json()["items"]
    assert [i["status"] for i in items] == ["failed", "sent"]
    log = client.get("/api/history").json()
    assert {l["result"] for l in log} == {"failed", "sent"}
    assert all(l["subject"] and l["created_at"] and l["recipient"] for l in log)


def test_uncertain_delivery_is_not_retried_and_stops_queue(client, provider):
    ids = setup_drafts(client, 3)
    approve(client, ids)
    provider.script = [providers.SendUncertain("timeout")]
    bid = prepare_and_confirm(client, ids)
    items = client.get(f"/api/send/batches/{bid}").json()["items"]
    assert [i["status"] for i in items] == ["uncertain", "cancelled", "cancelled"]
    batch = client.get(f"/api/send/batches/{bid}").json()["batch"]
    assert batch["status"] == "stopped"
    assert provider.sent == []  # no retry happened

    # The uncertain draft cannot be queued again until resolved.
    r = client.post("/api/send/prepare", json={"ids": [ids[0]]}).json()
    assert r["batch"] is None
    # Nor can a new draft for that address be generated.
    log = client.get("/api/history").json()
    entry = [l for l in log if l["result"] == "uncertain"][0]
    # User checks the Sent folder: it was not sent -> needs re-approval.
    client.post(f"/api/history/{entry['id']}/resolve", json={"resolution": "not_sent"})
    d = client.get(f"/api/drafts/{ids[0]}").json()
    assert d["approval_status"] == "pending" and d["send_status"] == "failed"


def test_auth_expiry_stops_queue_without_marking_sent(client, provider):
    ids = setup_drafts(client, 2)
    approve(client, ids)
    provider.script = [providers.AuthExpired("token revoked")]
    bid = prepare_and_confirm(client, ids)
    batch = client.get(f"/api/send/batches/{bid}").json()
    assert batch["batch"]["status"] == "stopped"
    assert all(i["status"] == "cancelled" for i in batch["items"])
    assert provider.sent == []
    # Not sent, still approved: can be sent later after reconnecting.
    assert client.get(f"/api/drafts/{ids[0]}").json()["send_status"] == "not_sent"


def test_rate_limit_stops_queue(client, provider):
    ids = setup_drafts(client, 3)
    approve(client, ids)
    provider.script = [None, providers.LimitReached("429")]
    bid = prepare_and_confirm(client, ids)
    items = client.get(f"/api/send/batches/{bid}").json()["items"]
    assert [i["status"] for i in items] == ["sent", "cancelled", "cancelled"]


def test_crash_mid_send_becomes_uncertain_on_restart(client):
    [did] = setup_drafts(client, 1)
    with db.transaction() as conn:
        bid = conn.execute("INSERT INTO send_batches(status, token, mode, provider, created_at) VALUES "
                           "('sending','','live','gmail',?)", (db.now(),)).lastrowid
        conn.execute("INSERT INTO send_items(batch_id, draft_id, approved_hash, status) VALUES (?,?,?,'sending')", (bid, did, "h"))
        conn.execute("UPDATE drafts SET send_status='sending' WHERE id=?", (did,))
    db.init_db()  # simulates restart
    assert client.get(f"/api/drafts/{did}").json()["send_status"] == "uncertain"
    assert client.get(f"/api/send/batches/{bid}").json()["batch"]["status"] == "stopped"


# ------------------------------------------------------- modes

def test_test_mode_only_sends_to_own_address(client, provider):
    set_settings(test_mode="true")
    ids = setup_drafts(client, 2)
    approve(client, ids)
    prepare_and_confirm(client, ids)
    assert sent_to(provider) == ["me@example.org", "me@example.org"]
    assert all(m["Subject"].startswith("[TEST]") for m in provider.sent)
    # Real drafts remain unsent and the real recipients are not marked contacted.
    assert client.get(f"/api/drafts/{ids[0]}").json()["send_status"] == "not_sent"
    assert {c["outreach_status"] for c in client.get("/api/contacts").json()} == {"drafted"}


def test_test_mode_requires_recipient(client):
    set_settings(test_mode="true", test_recipient="")
    [did] = setup_drafts(client, 1)
    approve(client, [did])
    r = client.post("/api/send/prepare", json={"ids": [did]})
    assert r.status_code == 400 and "test recipient" in r.json()["detail"]


def test_dry_run_sends_nothing(client, provider):
    set_settings(provider="dryrun")
    providers.set_provider_override(None)
    ids = setup_drafts(client, 2)
    approve(client, ids)
    prepare_and_confirm(client, ids)
    assert provider.sent == []
    assert {l["result"] for l in client.get("/api/history").json()} == {"simulated"}
