import pytest
from fastapi.testclient import TestClient

from app import config, db, providers, sender
from app.main import app

# Tests must never start a real website discovery (network) when the app starts.
config.DISCOVERY_AUTO_ON_STARTUP = False
# ...nor check the real Gmail inbox for replies.
config.REPLY_CHECK_AUTO = False


class FakeProvider:
    """Records messages instead of sending. `script` controls per-call outcomes."""
    name = "fake"

    def __init__(self):
        self.sent = []
        self.script = []  # list of exceptions (or None for success), consumed in order

    def check_ready(self):
        return None

    def send(self, msg):
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        self.sent.append(msg)
        return f"fake-{len(self.sent)}"


@pytest.fixture
def provider():
    p = FakeProvider()
    providers.set_provider_override(p)
    yield p
    providers.set_provider_override(None)


@pytest.fixture
def client(tmp_path, provider):
    db.set_path(tmp_path / "test.db")
    sender.SYNC = True
    with TestClient(app) as c:
        set_settings(provider="gmail", test_mode="false", send_delay_seconds="0", daily_send_limit="100",
                     sender_email="zehra@example.org", test_recipient="me@example.org")
        yield c
    sender.SYNC = False


def set_settings(**kwargs):
    with db.transaction() as conn:
        for k, v in kwargs.items():
            conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


CSV_HEADER = "Website Name,Website URL,Contact Name,Email,Niche,Relevant Article URL,Suggested Topic,Personalization Notes,Outreach Status,Email Verified\n"


def make_csv(rows):
    return (CSV_HEADER + "\n".join(rows) + "\n").encode()


def import_csv(client, rows):
    r = client.post("/api/import", files={"file": ("contacts.csv", make_csv(rows), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def contact_row(i, email=None, verified="yes", niche="Education"):
    email = email or f"editor{i}@site{i}.com"
    return f"Site {i},https://site{i}.com,Editor {i},{email},{niche},https://site{i}.com/category/online-learning,,,new,{verified}"


def setup_drafts(client, n=1, verified="yes"):
    """Import n contacts and generate their drafts. Returns draft ids."""
    import_csv(client, [contact_row(i, verified=verified) for i in range(n)])
    ids = [c["id"] for c in client.get("/api/contacts").json()]
    r = client.post("/api/drafts/generate", json={"ids": ids})
    assert r.status_code == 200, r.text
    return r.json()["created"]


def approve(client, ids):
    r = client.post("/api/drafts/approve", json={"ids": ids})
    assert r.status_code == 200, r.text
    return r.json()


def prepare_and_confirm(client, ids):
    r = client.post("/api/send/prepare", json={"ids": ids})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["batch"], data
    b = data["batch"]
    r = client.post("/api/send/confirm", json={"batch_id": b["id"], "token": b["token"], "confirm_text": "SEND"})
    assert r.status_code == 200, r.text
    return b["id"]
