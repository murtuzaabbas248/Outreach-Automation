"""Reply / bounce / follow-up-due alerts. Gmail is faked; nothing reads a real inbox or sends email."""
import json

import pytest

from app import config, db, gmail_auth, replies

from .conftest import approve, prepare_and_confirm, set_settings, setup_drafts

LATER = "2099-01-01T00:00:00+00:00"


class FakeGmail:
    def __init__(self):
        self.threads = {}   # thread id -> list of messages
        self.thread_for = {}  # sent message id -> thread id
        self.inbox = {}     # message id -> message

    def thread_of(self, mid):
        return self.thread_for.setdefault(mid, f"t-{mid}")

    def thread_messages(self, tid):
        return list(self.threads.get(tid, []))

    def inbox_ids(self, n):
        return list(self.inbox)[:n]

    def message(self, mid):
        return self.inbox[mid]


def msg(mid, frm, subject="Re: Guest post", date=LATER, snippet="Thanks, sounds good", auto=False):
    return {"id": mid, "thread_id": "", "from": frm, "subject": subject, "date": date, "snippet": snippet, "auto": auto}


@pytest.fixture
def sent(client, provider, monkeypatch):
    """Two live emails sent, a follow-up drafted for each, and a fake Gmail."""
    fake = FakeGmail()
    monkeypatch.setattr(replies, "_reader_override", fake)
    set_settings(followups_enabled="true")
    ids = setup_drafts(client, n=2)
    approve(client, ids)
    prepare_and_confirm(client, ids)
    assert len(provider.sent) == 2
    r = client.post("/api/followups/generate", json={"ids": ids, "delay_days": 7})
    assert r.status_code == 200, r.text
    with db.transaction() as conn:
        rows = db.rows(conn, "SELECT id, to_email, provider_message_id, contact_id FROM drafts WHERE kind='initial' ORDER BY id")
    yield fake, rows


def contact_status(email):
    with db.transaction() as conn:
        return conn.execute("SELECT outreach_status FROM contacts WHERE email_norm=?", (email,)).fetchone()[0]


def followup_state(email):
    with db.transaction() as conn:
        return conn.execute("SELECT draft_status FROM drafts WHERE kind='followup' AND to_email=?", (email,)).fetchone()[0]


def alerts():
    with db.transaction() as conn:
        return db.rows(conn, "SELECT * FROM notifications ORDER BY id")


def test_reply_in_thread_marks_replied_and_cancels_follow_up(sent, provider):
    fake, rows = sent
    first = rows[0]
    tid = fake.thread_of(first["provider_message_id"])
    fake.threads[tid] = [msg(first["provider_message_id"], "zehra@example.org", date="2000-01-01T00:00:00+00:00"),
                         msg("m-reply", first["to_email"])]
    r = replies.check("manual")
    assert r["checked"] and r["replies"] == 1 and r["bounces"] == 0
    assert contact_status(first["to_email"]) == "replied"
    assert followup_state(first["to_email"]) == "cancelled"          # never follow up someone who replied
    assert followup_state(rows[1]["to_email"]) != "cancelled"        # the other contact is untouched
    a = alerts()
    assert len(a) == 1 and a[0]["kind"] == "reply" and "Marked Replied" in a[0]["action"] and a[0]["read_at"] is None
    assert replies.check("manual")["replies"] == 0 and len(alerts()) == 1  # recorded once
    assert len(provider.sent) == 2                                     # checking never sends


def test_bounce_marks_bounced(sent):
    fake, rows = sent
    tid = fake.thread_of(rows[1]["provider_message_id"])
    fake.threads[tid] = [msg("m-bounce", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)", auto=True)]
    r = replies.check()
    assert r["bounces"] == 1 and contact_status(rows[1]["to_email"]) == "bounced"
    assert followup_state(rows[1]["to_email"]) == "cancelled"


def test_own_and_older_messages_are_not_replies(sent):
    fake, rows = sent
    tid = fake.thread_of(rows[0]["provider_message_id"])
    fake.threads[tid] = [msg("m-own", "zehra@example.org"), msg("m-old", rows[0]["to_email"], date="2000-01-01T00:00:00+00:00")]
    assert replies.check()["replies"] == 0 and alerts() == []
    assert contact_status(rows[0]["to_email"]) == "contacted"


def test_new_thread_reply_from_same_address_or_website_is_found(sent):
    fake, rows = sent
    site = rows[1]["to_email"].split("@")[1]
    fake.inbox = {"i1": msg("i1", f"jane@{site}", "Your pitch"),           # colleague at the same website
                  "i2": msg("i2", "stranger@gmail.com", "Hello"),          # unrelated
                  "i3": msg("i3", "newsletter@unrelated.com", "Sale")}
    r = replies.check()
    assert r["replies"] == 1 and contact_status(rows[1]["to_email"]) == "replied"
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM inbox_seen").fetchone()[0] == 3  # each inbox message read once
    assert replies.check()["replies"] == 0


def test_follow_up_due_alert_once(sent):
    fake, rows = sent
    with db.transaction() as conn:
        conn.execute("UPDATE drafts SET due_at='2000-01-01T00:00:00+00:00' WHERE kind='followup'")
    assert replies.check()["followups_due"] == 2
    assert replies.check()["followups_due"] == 0
    assert sum(a["kind"] == "followup_due" for a in alerts()) == 2


def test_bulk_delete_followups_keeps_sent_emails(sent, client):
    fake, rows = sent
    with db.transaction() as conn:
        conn.execute("UPDATE drafts SET due_at='2000-01-01T00:00:00+00:00' WHERE kind='followup'")
        followups = [r["id"] for r in conn.execute("SELECT id FROM drafts WHERE kind='followup' ORDER BY id")]
    replies.check()  # creates two 'follow-up due' alerts
    sent_id = rows[0]["id"]
    r = client.post("/api/drafts/delete", json={"ids": followups + [sent_id]}).json()
    assert sorted(r["deleted"]) == sorted(followups)
    assert [s["id"] for s in r["skipped"]] == [sent_id] and "cannot be deleted" in r["skipped"][0]["reason"]
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM drafts WHERE kind='followup'").fetchone()[0] == 0
        assert conn.execute("SELECT send_status FROM drafts WHERE id=?", (sent_id,)).fetchone()[0] == "sent"  # sent email kept
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='followup_due' AND read_at IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] >= 2  # history untouched


def test_api_list_check_and_mark_read(sent, client):
    fake, rows = sent
    fake.threads[fake.thread_of(rows[0]["provider_message_id"])] = [msg("m1", rows[0]["to_email"])]
    r = client.post("/api/notifications/check").json()
    assert r["checked"] and r["replies"] == 1 and "found" not in r
    n = client.get("/api/notifications").json()
    assert n["unread"] == 1 and n["ready"] and n["last_check"]["replies"] == 1
    client.post("/api/notifications/read", json={"ids": [n["items"][0]["id"]]})
    assert client.get("/api/notifications").json()["unread"] == 0


def test_send_only_token_explains_how_to_enable(tmp_path, monkeypatch):
    monkeypatch.setattr(replies, "_reader_override", None)
    token = tmp_path / "gmail_token.json"
    monkeypatch.setattr(config, "GMAIL_TOKEN_FILE", token)
    assert "not connected" in replies.ready()[1]
    token.write_text(json.dumps({"scopes": [config.GMAIL_SEND_SCOPE]}), encoding="utf-8")
    ok, why = replies.ready()
    assert not ok and "Allow reply checking" in why and not gmail_auth.can_read_replies()
    assert replies.check()["checked"] is False
    token.write_text(json.dumps({"scopes": [config.GMAIL_SEND_SCOPE, config.GMAIL_METADATA_SCOPE]}), encoding="utf-8")
    assert replies.ready() == (True, "") and gmail_auth.status()["reply_checking"]


def test_sending_uses_the_tokens_own_scopes(tmp_path, monkeypatch):
    """An older send-only token must keep working: never ask Google for scopes it wasn't granted."""
    from google.oauth2 import credentials as gcreds

    from app import providers
    token = tmp_path / "gmail_token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "GMAIL_TOKEN_FILE", token)
    seen = {}

    class Fake:
        valid = True

    def fake_load(path, *args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return Fake()

    monkeypatch.setattr(gcreds.Credentials, "from_authorized_user_file", staticmethod(fake_load))
    providers.GmailProvider()._credentials()
    assert seen == {"args": (), "kwargs": {}}


def test_connect_requests_send_and_readonly_without_metadata():
    # Google refuses full-message reads while a token carries gmail.metadata, so it is never requested now.
    assert config.GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]


@pytest.mark.parametrize("scopes,checking,text", [
    ([config.GMAIL_SEND_SCOPE], False, False),
    ([config.GMAIL_SEND_SCOPE, config.GMAIL_METADATA_SCOPE], True, False),
    ([config.GMAIL_SEND_SCOPE, config.GMAIL_READONLY_SCOPE], True, True),
    ([config.GMAIL_SEND_SCOPE, config.GMAIL_READONLY_SCOPE, config.GMAIL_METADATA_SCOPE], True, False),
])
def test_scope_combinations(tmp_path, monkeypatch, scopes, checking, text):
    token = tmp_path / "t.json"
    token.write_text(json.dumps({"scopes": scopes}), encoding="utf-8")
    monkeypatch.setattr(config, "GMAIL_TOKEN_FILE", token)
    assert gmail_auth.can_read_replies() is checking and gmail_auth.can_read_reply_text() is text


def b64(s):
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_message_text_prefers_plain_then_html():
    plain = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("Hi Zehra,\nYes please send it.")}},
        {"mimeType": "text/html", "body": {"data": b64("<p>ignored</p>")}}]}
    assert replies.message_text(plain) == "Hi Zehra,\nYes please send it."
    html = {"mimeType": "text/html", "body": {"data": b64("<p>Our fee is <b>$50</b>.</p><script>x()</script>")}}
    assert "Our fee is" in replies.message_text(html) and "x()" not in replies.message_text(html)


def test_strip_quoted_keeps_only_their_new_words():
    text = ("Sounds great, send the draft by Friday.\n\nThanks,\nAmy\n\n"
            "On Mon, Nov 23, 2026 at 5:12 PM Zehra Zaidi <zehra@example.org>\nwrote:\n> Hi Amy,\n> I'd like to pitch...")
    assert replies.strip_quoted(text) == "Sounds great, send the draft by Friday.\n\nThanks,\nAmy"
    assert replies.strip_quoted("> only quoted") == "> only quoted"  # never returns empty


def test_view_reply_shows_text_next_to_your_email(sent, client):
    fake, rows = sent
    first = rows[0]
    fake.threads[fake.thread_of(first["provider_message_id"])] = [msg("m-r", first["to_email"])]
    calls = []

    def full_message(mid):
        calls.append(mid)
        return {**msg(mid, first["to_email"]), "text": "We accept! Our editor will review.\n\nOn Mon wrote:\n> pitch"}

    fake.full_message = full_message
    replies.check()
    nid = client.get("/api/notifications").json()["items"][0]["id"]
    r = client.get(f"/api/notifications/{nid}/message").json()
    assert r["reply_only"] == "We accept! Our editor will review."
    assert r["yours"]["to_email"] == first["to_email"] and r["yours"]["body"]
    assert r["gmail_url"].endswith("/m-r") and r["needs_permission"] is False
    client.get(f"/api/notifications/{nid}/message")
    assert calls == ["m-r"]                                   # fetched from Gmail once, then cached
    assert client.get("/api/notifications").json()["unread"] == 0  # opening it marks it read
    fo = client.get("/api/followups").json()["sent_originals"]
    assert next(d for d in fo if d["id"] == first["id"])["reply_id"] == nid
    assert client.get("/api/notifications/99999/message").status_code == 404


def test_view_without_readonly_explains_permission(sent, client, tmp_path, monkeypatch):
    fake, rows = sent
    fake.threads[fake.thread_of(rows[0]["provider_message_id"])] = [msg("m-r", rows[0]["to_email"])]
    replies.check()
    nid = client.get("/api/notifications").json()["items"][0]["id"]
    monkeypatch.setattr(replies, "_reader_override", None)
    token = tmp_path / "t.json"
    token.write_text(json.dumps({"scopes": [config.GMAIL_SEND_SCOPE, config.GMAIL_METADATA_SCOPE]}), encoding="utf-8")
    monkeypatch.setattr(config, "GMAIL_TOKEN_FILE", token)
    r = client.get(f"/api/notifications/{nid}/message").json()
    assert r["needs_permission"] and "Allow reading replies" in r["message"] and r["text"] == ""
    assert r["yours"]["body"]  # your own email is still shown
