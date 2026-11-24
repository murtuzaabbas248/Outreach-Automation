"""Email providers.

Each provider's send() returns a provider message id or raises one of:
  * SendRejected     - provider definitely did NOT accept the email (safe to fix and try later)
  * SendUncertain    - we cannot tell whether it was accepted (never retried automatically)
  * AuthExpired      - credentials missing/expired; queue stops (email not sent)
  * LimitReached     - provider rate/daily limit; queue stops (email not sent)
"""
import base64
import smtplib
import socket
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config


class ProviderError(Exception):
    pass


class SendRejected(ProviderError):
    pass


class SendUncertain(ProviderError):
    pass


class AuthExpired(ProviderError):
    pass


class LimitReached(ProviderError):
    pass


def build_message(sender_name: str, sender_email: str, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email)) if sender_email else sender_name
    msg["To"] = to  # exactly one recipient per message, never CC/BCC lists
    msg["Subject"] = subject
    domain = sender_email.split("@")[-1] if "@" in sender_email else "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(body)
    return msg


class DryRunProvider:
    """Sends nothing. Useful for trying the whole workflow safely."""
    name = "dryrun"

    def check_ready(self) -> None:
        return None

    def send(self, msg: EmailMessage) -> str:
        return f"dryrun-{uuid.uuid4().hex[:12]}"


class GmailProvider:
    name = "gmail"

    def __init__(self):
        self._service = None

    def _credentials(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not config.GMAIL_TOKEN_FILE.exists():
            raise AuthExpired("Gmail is not connected. Open Settings and click 'Connect Gmail'.")
        # Use the scopes the token was actually granted (stored in the file). Asking for more on
        # refresh would make Google reject an older send-only token and break sending.
        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN_FILE))
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as exc:
                    raise AuthExpired("Gmail authorization expired or was revoked. Reconnect Gmail in Settings.") from exc
                except Exception as exc:  # network problem while refreshing: nothing was sent
                    raise AuthExpired(f"Could not refresh Gmail authorization: {type(exc).__name__}") from exc
                config.GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise AuthExpired("Gmail authorization is invalid. Reconnect Gmail in Settings.")
        return creds

    def check_ready(self) -> None:
        self._credentials()

    def send(self, msg: EmailMessage) -> str:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        creds = self._credentials()  # raises AuthExpired before anything is sent
        if self._service is None:
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        try:
            # num_retries=0: never let the client library retry a send on its own.
            result = self._service.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=0)
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            text = str(exc).lower()
            if status == 401:
                raise AuthExpired("Gmail rejected the credentials (401). Reconnect Gmail.") from exc
            if status == 429 or any(k in text for k in ("ratelimitexceeded", "dailylimitexceeded", "userratelimitexceeded", "quota")):
                raise LimitReached(f"Gmail sending limit reached (HTTP {status}).") from exc
            if status == 403:
                raise AuthExpired(f"Gmail refused permission (403): {exc.reason}") from exc
            if 400 <= status < 500:
                raise SendRejected(f"Gmail rejected the message (HTTP {status}): {exc.reason}") from exc
            raise SendUncertain(f"Gmail server error (HTTP {status}); the message may or may not have been sent.") from exc
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise SendUncertain(f"Network problem while sending ({type(exc).__name__}); delivery status unknown.") from exc
        return result.get("id", "")


class SmtpProvider:
    name = "smtp"

    def check_ready(self) -> None:
        if not (config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD):
            raise AuthExpired("SMTP is not configured. Set SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD in your .env file.")

    def send(self, msg: EmailMessage) -> str:
        self.check_ready()
        # Connection and login happen before any message data is transmitted,
        # so failures here mean the email was definitely not sent.
        try:
            if config.SMTP_USE_SSL:
                server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT)
            else:
                server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT)
                server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        except smtplib.SMTPAuthenticationError as exc:
            raise AuthExpired("SMTP login failed. Check SMTP_USERNAME / SMTP_PASSWORD (use an app password).") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise AuthExpired(f"Could not connect to the SMTP server ({type(exc).__name__}). Nothing was sent.") from exc

        try:
            refused = server.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendRejected("The server refused the recipient address.") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SendRejected(f"The server refused the sender address ({exc.smtp_code}).") from exc
        except smtplib.SMTPDataError as exc:
            detail = (exc.smtp_error or b"").decode(errors="replace").lower()
            if exc.smtp_code in (421, 450, 451, 452, 454, 550, 554) and any(k in detail for k in ("limit", "quota", "rate")):
                raise LimitReached(f"SMTP sending limit reached ({exc.smtp_code}).") from exc
            raise SendRejected(f"The server rejected the message ({exc.smtp_code}).") from exc
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, OSError, smtplib.SMTPException) as exc:
            raise SendUncertain(f"Connection lost while sending ({type(exc).__name__}); delivery status unknown.") from exc
        finally:
            try:
                server.quit()
            except Exception:
                pass
        if refused:
            raise SendRejected("The server refused the recipient address.")
        return msg["Message-ID"]


_override = None


def set_provider_override(provider) -> None:
    """Tests inject a fake provider here."""
    global _override
    _override = provider


def get_provider(name: str):
    if _override is not None:
        return _override
    if name == "gmail":
        return GmailProvider()
    if name == "smtp":
        return SmtpProvider()
    return DryRunProvider()
