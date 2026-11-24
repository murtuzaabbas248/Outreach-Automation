"""Gmail OAuth 2.0 connection (Desktop app flow).

Run directly to connect from the terminal:
    python -m app.gmail_auth
"""
import threading

from . import config

_state = {"running": False, "error": ""}


def granted_scopes() -> set[str]:
    """Scopes recorded in the saved token (empty if not connected or unreadable)."""
    import json
    try:
        data = json.loads(config.GMAIL_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    scopes = data.get("scopes") or []
    return set(scopes.split() if isinstance(scopes, str) else scopes)


READ_SCOPES = {config.GMAIL_METADATA_SCOPE, "https://www.googleapis.com/auth/gmail.readonly",
               "https://www.googleapis.com/auth/gmail.modify", "https://mail.google.com/"}


FULL_READ_SCOPES = {config.GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify", "https://mail.google.com/"}


def can_read_replies() -> bool:
    return bool(granted_scopes() & READ_SCOPES)


def can_read_reply_text() -> bool:
    """Full message text needs read-only (or broader) access, and Google refuses it while the
    token also carries gmail.metadata."""
    g = granted_scopes()
    return bool(g & FULL_READ_SCOPES) and config.GMAIL_METADATA_SCOPE not in g


# ---- web sign-in (hosted app): redirect to Google, come back to /oauth2callback ----
_pending: dict[str, str | None] = {}  # OAuth state -> PKCE code verifier


def web_mode() -> bool:
    return bool(config.PUBLIC_BASE_URL)


def client_secret_present() -> bool:
    return bool(config.GMAIL_CLIENT_SECRET_JSON.strip()) or config.GMAIL_CLIENT_SECRET_FILE.exists()


def _client_config() -> dict:
    import json
    if config.GMAIL_CLIENT_SECRET_JSON.strip():
        return json.loads(config.GMAIL_CLIENT_SECRET_JSON)
    return json.loads(config.GMAIL_CLIENT_SECRET_FILE.read_text(encoding="utf-8"))


def _redirect_uri() -> str:
    return config.PUBLIC_BASE_URL + "/oauth2callback"


def start_web_flow() -> str:
    """Return the Google sign-in URL. Google sends the user back to PUBLIC_BASE_URL/oauth2callback."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(_client_config(), scopes=config.GMAIL_SCOPES, redirect_uri=_redirect_uri())
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _pending[state] = getattr(flow, "code_verifier", None)
    if len(_pending) > 20:
        _pending.pop(next(iter(_pending)))
    return url


def finish_web_flow(state: str, code: str) -> None:
    from google_auth_oauthlib.flow import Flow
    if state not in _pending:
        raise ValueError("This Google sign-in link expired. Click Connect Gmail again.")
    verifier = _pending.pop(state)
    flow = Flow.from_client_config(_client_config(), scopes=config.GMAIL_SCOPES, redirect_uri=_redirect_uri(), state=state)
    if verifier:
        flow.code_verifier = verifier
    flow.fetch_token(code=code)
    config.GMAIL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN_FILE.write_text(flow.credentials.to_json(), encoding="utf-8")


def status() -> dict:
    connected = config.GMAIL_TOKEN_FILE.exists()
    return {
        "client_secret_present": client_secret_present(),
        "web_mode": web_mode(),
        "connected": connected,
        "connecting": _state["running"],
        "error": _state["error"],
        "reply_checking": connected and can_read_replies(),
        "reply_text": connected and can_read_reply_text(),
    }


def connect_blocking() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GMAIL_CLIENT_SECRET_FILE.exists():
        raise FileNotFoundError(
            f"OAuth client file not found at {config.GMAIL_CLIENT_SECRET_FILE}. See README section 'Gmail API setup'."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CLIENT_SECRET_FILE), config.GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300,
                                  authorization_prompt_message="Opening your browser to connect Gmail...",
                                  success_message="Gmail connected. You can close this tab and return to the dashboard.")
    config.GMAIL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def connect_in_background() -> None:
    if _state["running"]:
        return

    def _run():
        _state.update(running=True, error="")
        try:
            connect_blocking()
        except Exception as exc:
            _state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _state["running"] = False

    threading.Thread(target=_run, daemon=True).start()


def disconnect() -> None:
    if config.GMAIL_TOKEN_FILE.exists():
        config.GMAIL_TOKEN_FILE.unlink()


if __name__ == "__main__":
    connect_blocking()
    print(f"Connected. Token saved to {config.GMAIL_TOKEN_FILE}")
