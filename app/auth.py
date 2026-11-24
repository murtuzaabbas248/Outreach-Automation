"""Login for the hosted app.

When APP_PASSWORD is set, every page and API call needs a signed login cookie. On a server
(Render, or PUBLIC_BASE_URL set) without APP_PASSWORD, the app refuses all requests, because an
open dashboard would let anyone send email from your Gmail. Running locally without a password
works as before.

The cookie is HMAC-signed with SESSION_SECRET (or a key derived from APP_PASSWORD) and expires after
SESSION_DAYS. Failed logins are slowed down per client address.
"""
import hashlib
import hmac
import secrets
import time

from . import config

COOKIE = "outreach_session"
OPEN_PATHS = ("/login", "/logout", "/healthz", "/static/app.css")
_failures: dict[str, list[float]] = {}


def enabled() -> bool:
    return bool(config.APP_PASSWORD)


def misconfigured() -> bool:
    return config.ON_SERVER and not config.APP_PASSWORD


def _key() -> bytes:
    base = config.SESSION_SECRET or ("pw:" + config.APP_PASSWORD)
    return hashlib.sha256(base.encode()).digest()


def make_token() -> str:
    exp = str(int(time.time()) + config.SESSION_DAYS * 86400)
    nonce = secrets.token_hex(8)
    sig = hmac.new(_key(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{nonce}.{sig}"


def valid_token(token: str | None) -> bool:
    try:
        exp, nonce, sig = (token or "").split(".")
        good = hmac.new(_key(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except ValueError:
        return False


def check_password(password: str, client: str) -> tuple[bool, str]:
    now = time.time()
    recent = [t for t in _failures.get(client, []) if now - t < 900]
    if len(recent) >= 8:
        return False, "Too many attempts. Wait 15 minutes and try again."
    if enabled() and hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode()):
        _failures.pop(client, None)
        return True, ""
    _failures[client] = recent + [now]
    return False, "Wrong password."


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path == "/oauth2callback"


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · Outreach Dashboard</title>
<link rel="stylesheet" href="/static/app.css"></head>
<body style="display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
<form method="post" action="/login" class="card" style="width:100%;max-width:360px;margin:16px">
<h2 style="margin-top:0">Outreach Dashboard</h2>
<p class="small muted">Enter the app password to continue.</p>
{error}
<div class="field"><label for="pw">Password</label>
<input type="password" id="pw" name="password" autofocus required autocomplete="current-password"></div>
<button class="btn primary" type="submit" style="width:100%">Sign in</button>
</form></body></html>"""

MISCONFIGURED_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Setup needed</title></head>
<body style="font-family:Arial;max-width:560px;margin:60px auto;padding:0 16px">
<h2>Setup needed</h2><p>This app is running on a server but <b>APP_PASSWORD</b> is not set, so it is locked to keep your
Gmail safe. Add APP_PASSWORD (and SESSION_SECRET) in your hosting environment settings, then restart.</p></body></html>"""
