"""Hosting: login page, server lock without a password, Gmail web sign-in, backup and restore."""
import sqlite3

import pytest

from app import auth, config, db, gmail_auth


@pytest.fixture
def locked(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    auth._failures.clear()
    yield client
    client.cookies.clear()


def test_local_without_password_needs_no_login(client):
    assert client.get("/api/stats").status_code == 200


def test_everything_needs_login_when_password_set(locked):
    c = locked
    assert c.get("/api/stats").status_code == 401
    assert c.post("/api/send/prepare", json={"ids": [1]}).status_code == 401  # can't send without signing in
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/healthz").status_code == 200 and c.get("/login").status_code == 200


def test_login_wrong_then_right_password(locked):
    c = locked
    r = c.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert "Wrong" in r.headers["location"] and auth.COOKIE not in r.cookies
    r = c.post("/login", data={"password": "correct horse battery staple"}, follow_redirects=False)
    assert r.status_code == 303 and r.cookies.get(auth.COOKIE)
    assert c.get("/api/stats").status_code == 200
    assert c.get("/api/meta").json()["login_enabled"] is True
    c.get("/logout")
    c.cookies.clear()
    assert c.get("/api/stats").status_code == 401


def test_forged_or_expired_cookie_is_rejected(locked, monkeypatch):
    good = auth.make_token()
    exp, nonce, sig = good.split(".")
    assert not auth.valid_token(f"{exp}.{nonce}.{'0' * len(sig)}")
    assert not auth.valid_token(f"{int(exp) + 999}.{nonce}.{sig}")  # changed expiry breaks the signature
    monkeypatch.setattr(config, "SESSION_DAYS", -1)
    assert not auth.valid_token(auth.make_token())
    assert not auth.valid_token("garbage") and not auth.valid_token(None)


def test_password_guessing_is_limited(locked):
    for _ in range(8):
        auth.check_password("bad", "1.2.3.4")
    assert "Too many attempts" in auth.check_password("correct horse battery staple", "1.2.3.4")[1]
    assert auth.check_password("correct horse battery staple", "5.6.7.8")[0]


def test_server_without_password_is_locked(client, monkeypatch):
    monkeypatch.setattr(config, "ON_SERVER", True)
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    assert client.get("/api/stats").status_code == 503
    assert "APP_PASSWORD" in client.get("/").text
    assert client.get("/healthz").status_code == 200


def test_gmail_web_sign_in_url(client, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://outreach.example.com")
    monkeypatch.setattr(config, "GMAIL_CLIENT_SECRET_JSON",
                        '{"web": {"client_id": "abc.apps.googleusercontent.com", "client_secret": "s", '
                        '"auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')
    r = client.post("/api/gmail/connect").json()
    url = r["auth_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "redirect_uri=https%3A%2F%2Foutreach.example.com%2Foauth2callback" in url
    assert "gmail.send" in url and "gmail.readonly" in url and "access_type=offline" in url
    bad = client.get("/oauth2callback", params={"state": "unknown", "code": "x"})
    assert bad.status_code == 400 and "try again" in bad.text
    assert client.get("/oauth2callback", params={"error": "access_denied"}).status_code == 400


def test_backup_and_restore_round_trip(client, tmp_path):
    from .conftest import contact_row, import_csv
    import_csv(client, [contact_row(1), contact_row(2)])
    backup = client.get("/api/admin/backup.db")
    assert backup.status_code == 200 and backup.content.startswith(b"SQLite format 3\x00")
    import_csv(client, [contact_row(3)])
    assert len(client.get("/api/contacts").json()) == 3
    files = {"file": ("outreach.db", backup.content, "application/octet-stream")}
    assert client.post("/api/admin/restore", files=files, data={"confirm_text": "nope"}).status_code == 400
    r = client.post("/api/admin/restore", files=files, data={"confirm_text": "REPLACE"}).json()
    assert r["restored"] and r["counts"]["contacts"] == 2
    assert len(client.get("/api/contacts").json()) == 2
    assert (db.get_path().parent / "backups" / r["previous_saved_as"]).exists()  # previous data kept next to the DB
    junk = {"file": ("x.db", b"not a database", "application/octet-stream")}
    assert client.post("/api/admin/restore", files=junk, data={"confirm_text": "REPLACE"}).status_code == 400
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).execute("CREATE TABLE t(x)").connection.commit()
    r = client.post("/api/admin/restore", files={"file": ("e.db", empty.read_bytes(), "application/octet-stream")},
                    data={"confirm_text": "REPLACE"})
    assert r.status_code == 400 and "missing tables" in r.json()["detail"]
