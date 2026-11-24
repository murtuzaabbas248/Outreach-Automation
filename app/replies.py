"""Reply, bounce and follow-up-due alerts.

* Detection reads only headers (From, Subject, Date). A reply's text is downloaded only when you click
  "View reply" (needs gmail.readonly), and only for messages already identified as replies to your
  outreach. Nothing is sent, deleted or changed in Gmail.
* For every live email the app sent, it looks at the Gmail thread for messages from someone else
  (a reply) or from a mail server (a bounce). It also scans recent Inbox messages for new emails
  from an address (or a non-webmail domain) you contacted, since some people reply in a new thread.
* A reply marks the contact Replied and cancels its pending follow-ups (existing contacts logic).
  A bounce marks it Bounced and cancels its pending drafts. Each alert is recorded once.
* Follow-ups whose due date has passed and that still wait for approval raise a "follow-up due" alert.
  The app never sends them by itself; approval and typed confirmation stay with you.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from . import config, contacts, db, gmail_auth
from .domains import WEBMAIL, root_domain
from .validation import normalize_email

BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|mailerdaemon)@", re.I)
BOUNCE_SUBJECT = re.compile(r"delivery status notification|undeliver|mail delivery (failed|subsystem)|"
                            r"returned mail|delivery (has )?failed|failure notice|address not found", re.I)
CLOSED_STATUSES = ("replied", "opted_out", "do_not_contact", "not_interested", "bounced")
LAST_CHECK_KEY = "replies_last_check"
INBOX_SCAN = 100

_lock = threading.Lock()
_reader_override = None  # tests inject a fake reader


class GmailReader:
    """Header-only Gmail access (gmail.metadata)."""
    HEADERS = ["From", "Subject", "Date", "Auto-Submitted"]

    def __init__(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN_FILE))  # granted scopes only
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            config.GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False).users()

    @classmethod
    def _msg(cls, m: dict) -> dict:
        h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers", [])}
        ts = datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, timezone.utc).replace(microsecond=0).isoformat()
        return {"id": m["id"], "thread_id": m.get("threadId", ""), "from": parseaddr(h.get("from", ""))[1].lower(),
                "subject": h.get("subject", ""), "date": ts, "snippet": m.get("snippet", ""),
                "auto": h.get("auto-submitted", "no").lower() != "no"}

    def thread_of(self, message_id: str) -> str:
        return self.svc.messages().get(userId="me", id=message_id, format="minimal").execute()["threadId"]

    def thread_messages(self, thread_id: str) -> list[dict]:
        t = self.svc.threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=self.HEADERS).execute()
        return [self._msg(m) for m in t.get("messages", [])]

    def inbox_ids(self, n: int) -> list[str]:
        r = self.svc.messages().list(userId="me", labelIds=["INBOX"], maxResults=n).execute()
        return [m["id"] for m in r.get("messages", [])]

    def message(self, message_id: str) -> dict:
        return self._msg(self.svc.messages().get(userId="me", id=message_id, format="metadata",
                                                metadataHeaders=self.HEADERS).execute())

    def full_message(self, message_id: str) -> dict:
        """Headers plus the message text (needs gmail.readonly). Read-only: nothing is changed in Gmail."""
        m = self.svc.messages().get(userId="me", id=message_id, format="full").execute()
        out = self._msg(m)
        out["text"] = message_text(m.get("payload") or {})
        return out


def _b64(data: str) -> str:
    import base64
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4)).decode("utf-8", "replace")


def message_text(payload: dict) -> str:
    """The plain-text body of a Gmail message payload, else its HTML converted to text."""
    plain, html = [], []

    def walk(p):
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            html.append(_b64(data))
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("\n".join(html), "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    return ""


QUOTE_START = re.compile(r"^(On [^\n]{1,200}(\n[^\n]{0,120})?wrote:|-{2,}\s*Original Message\s*-{2,}|From: [^\n]+|_{5,})\s*$",
                         re.I | re.M)


def strip_quoted(text: str) -> str:
    """Their new words only: cut at the quoted earlier message ("On ... wrote:", "> ..." lines)."""
    m = QUOTE_START.search(text or "")
    head = text[:m.start()] if m else (text or "")
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip() or (text or "").strip()


def get_reader():
    return _reader_override if _reader_override is not None else GmailReader()


def ready() -> tuple[bool, str]:
    if _reader_override is not None:
        return True, ""
    if not config.GMAIL_TOKEN_FILE.exists():
        return False, "Gmail is not connected. Connect Gmail in Settings to get reply alerts."
    if not gmail_auth.can_read_replies():
        return False, ("Gmail is connected for sending only. Click 'Allow reply checking' to reconnect Gmail with "
                       "header-only read access (sender, subject and date; never message text).")
    return True, ""


# ------------------------------------------------------------------ recording

def _is_bounce(m: dict) -> bool:
    return bool(BOUNCE_FROM.match(m["from"])) or bool(BOUNCE_SUBJECT.search(m["subject"]) and m["auto"]) \
        or bool(BOUNCE_SUBJECT.search(m["subject"]) and BOUNCE_FROM.match(m["from"]))


def _record(kind: str, m: dict, sent: dict) -> dict | None:
    """Store one alert (once per Gmail message) and update the contact. Returns the alert or None."""
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications(key, kind, contact_id, draft_id, email, from_addr, subject, snippet, "
            "received_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (m["id"], kind, sent.get("contact_id"), sent.get("draft_id"), sent["email"], m["from"], m["subject"][:300],
             m["snippet"][:300], m["date"], db.now()))
        if not cur.rowcount:
            return None
        nid = cur.lastrowid
        c = db.row(conn, "SELECT id, outreach_status FROM contacts WHERE id=?", (sent.get("contact_id"),)) \
            if sent.get("contact_id") else None
    action = "Recorded"
    if c and c["outreach_status"] not in CLOSED_STATUSES:
        res = contacts.set_status(c["id"], "replied" if kind == "reply" else "bounced")  # cancels pending follow-ups
        n = res.get("drafts_cancelled", 0)
        action = f"Marked {'Replied' if kind == 'reply' else 'Bounced'}" + (f"; {n} pending draft(s) cancelled" if n else "")
    elif c:
        action = f"Contact already marked {c['outreach_status'].replace('_', ' ')}"
    with db.transaction() as conn:
        conn.execute("UPDATE notifications SET action=? WHERE id=?", (action, nid))
    return {"id": nid, "kind": kind, "email": sent["email"], "from": m["from"], "action": action}


def _sent_emails(conn) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.REPLY_CHECK_DAYS)).replace(microsecond=0).isoformat()
    return db.rows(conn,
        "SELECT d.id AS draft_id, d.contact_id, d.to_email AS email, d.sent_at, d.provider_message_id, t.thread_id "
        "FROM drafts d LEFT JOIN email_threads t ON t.draft_id=d.id "
        "WHERE d.send_status='sent' AND d.provider_message_id IS NOT NULL AND d.sent_at>=? "
        "AND EXISTS (SELECT 1 FROM send_log l WHERE l.draft_id=d.id AND l.mode='live' AND l.result='sent') "
        "ORDER BY d.sent_at", (cutoff,))


def check(trigger: str = "manual") -> dict:
    """Look for replies and bounces now. Never sends anything."""
    ok, why = ready()
    if not ok:
        return {"checked": False, "message": why}
    if not _lock.acquire(blocking=False):
        return {"checked": False, "message": "A reply check is already running."}
    found, errors = [], []
    try:
        reader = get_reader()
        with db.transaction() as conn:
            sender = normalize_email(db.get_settings(conn).get("sender_email", ""))
            sent = _sent_emails(conn)
        own_ids = {s["provider_message_id"] for s in sent}

        # 1. Replies and bounces in the threads of emails you sent.
        for s in sent:
            try:
                tid = s["thread_id"]
                if not tid:
                    tid = reader.thread_of(s["provider_message_id"])
                    with db.transaction() as conn:
                        conn.execute("INSERT OR REPLACE INTO email_threads(draft_id, thread_id) VALUES (?,?)", (s["draft_id"], tid))
                for m in reader.thread_messages(tid):
                    if m["id"] in own_ids or m["from"] == sender or m["date"] <= (s["sent_at"] or ""):
                        continue
                    r = _record("bounce" if _is_bounce(m) else "reply", m, s)
                    if r:
                        found.append(r)
            except Exception as exc:  # one thread must not stop the check
                errors.append(f"{s['email']}: {type(exc).__name__}")

        # 2. Replies that started a new conversation in your Inbox.
        by_addr, by_domain = {}, {}
        for s in sent:
            by_addr.setdefault(normalize_email(s["email"]), s)
            d = root_domain(s["email"])
            if d and d not in WEBMAIL:
                by_domain.setdefault(d, s)
        try:
            ids = reader.inbox_ids(INBOX_SCAN)
            with db.transaction() as conn:
                seen = {r["message_id"] for r in conn.execute(
                    f"SELECT message_id FROM inbox_seen WHERE message_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
            for mid in ids:
                if mid in seen or mid in own_ids:
                    continue
                m = reader.message(mid)
                with db.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO inbox_seen(message_id, seen_at) VALUES (?,?)", (mid, db.now()))
                s = by_addr.get(m["from"]) or by_domain.get(root_domain(m["from"]))
                if s and m["date"] > (s["sent_at"] or "") and m["from"] != sender:
                    r = _record("bounce" if _is_bounce(m) else "reply", m, s)
                    if r:
                        found.append(r)
        except Exception as exc:
            errors.append(f"Inbox: {type(exc).__name__}")

        due = record_due_followups()
        summary = {"checked": True, "at": db.now(), "trigger": trigger, "watched": len(sent),
                   "replies": sum(r["kind"] == "reply" for r in found), "bounces": sum(r["kind"] == "bounce" for r in found),
                   "followups_due": due, "errors": errors[:10]}
        summary["message"] = (f"Checked {len(sent)} sent email(s): {summary['replies']} new repl(ies), "
                              f"{summary['bounces']} bounce(s), {due} follow-up(s) newly due.")
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (LAST_CHECK_KEY, json.dumps(summary)))
        return summary | {"found": found}
    except Exception as exc:
        return {"checked": False, "message": f"Reply check failed: {type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        _lock.release()


def record_due_followups() -> int:
    """One alert per follow-up whose due date has passed and that is still waiting for you."""
    n = 0
    with db.transaction() as conn:
        rows = db.rows(conn,
            "SELECT d.id, d.contact_id, d.to_email, d.subject, d.due_at FROM drafts d "
            "LEFT JOIN contacts c ON c.id=d.contact_id "
            "WHERE d.kind='followup' AND d.draft_status!='cancelled' AND d.send_status IN ('not_sent','failed') "
            "AND d.due_at IS NOT NULL AND d.due_at<=? AND (c.outreach_status IS NULL OR c.outreach_status NOT IN "
            "('replied','opted_out','do_not_contact','not_interested','bounced'))", (db.now(),))
        for d in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notifications(key, kind, contact_id, draft_id, email, subject, received_at, created_at, action) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (f"followup:{d['id']}", "followup_due", d["contact_id"], d["id"], d["to_email"],
                                               d["subject"][:300], d["due_at"], db.now(), "Waiting for your approval"))
            n += cur.rowcount
    return n


# ------------------------------------------------------------------ dashboard

def summary(limit: int = 100) -> dict:
    with db.transaction() as conn:
        items = db.rows(conn, "SELECT n.*, c.website_name, c.outreach_status FROM notifications n "
                              "LEFT JOIN contacts c ON c.id=n.contact_id ORDER BY n.id DESC LIMIT ?", (limit,))
        unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]
        last = db.get_settings(conn).get(LAST_CHECK_KEY)
    ok, why = ready()
    for it in items:
        it.pop("body", None)  # text is loaded per reply via view()
    return {"unread": unread, "items": items, "ready": ok, "problem": why,
            "reply_text": _reader_override is not None or gmail_auth.can_read_reply_text(),
            "last_check": json.loads(last) if last else None, "interval_minutes": config.REPLY_CHECK_MINUTES}


def view(notification_id: int) -> dict:
    """A reply (or bounce) with its text, next to the email you sent. The text is fetched from Gmail
    only when you ask, cached, and the alert is marked read."""
    with db.transaction() as conn:
        n = db.row(conn, "SELECT n.*, c.website_name, c.contact_name FROM notifications n "
                         "LEFT JOIN contacts c ON c.id=n.contact_id WHERE n.id=?", (notification_id,))
        if not n:
            raise ValueError("Alert not found.")
        yours = db.row(conn, "SELECT id, kind, to_email, subject, body, sent_at FROM drafts WHERE id=?", (n["draft_id"],)) \
            if n["draft_id"] else None
    out = {"id": n["id"], "kind": n["kind"], "website_name": n["website_name"] or "", "contact_name": n["contact_name"] or "",
           "email": n["email"], "from": n["from_addr"], "subject": n["subject"], "received_at": n["received_at"],
           "action": n["action"], "yours": yours, "text": n["body"], "reply_only": strip_quoted(n["body"]) if n["body"] else "",
           "gmail_url": f"https://mail.google.com/mail/u/0/#all/{n['key']}" if n["kind"] != "followup_due" else "",
           "needs_permission": False, "message": ""}
    if n["kind"] != "followup_due" and not n["body"]:
        if _reader_override is None and not gmail_auth.can_read_reply_text():
            out.update(needs_permission=True, message="To show the reply's text here, click 'Allow reading replies' and approve "
                                                      "read-only Gmail access. Until then, use Open in Gmail.")
        else:
            try:
                full = get_reader().full_message(n["key"])
                text = (full.get("text") or "").strip()[:20000]
                with db.transaction() as conn:
                    conn.execute("UPDATE notifications SET body=?, snippet=CASE WHEN snippet='' THEN ? ELSE snippet END WHERE id=?",
                                 (text, (full.get("snippet") or "")[:300], n["id"]))
                out.update(text=text, reply_only=strip_quoted(text))
            except Exception as exc:
                out["message"] = f"Couldn't load the message from Gmail ({type(exc).__name__}). Use Open in Gmail."
    mark_read([n["id"]])
    return out


def mark_read(ids: list[int] | None = None) -> int:
    with db.transaction() as conn:
        if ids:
            cur = conn.execute(f"UPDATE notifications SET read_at=? WHERE read_at IS NULL AND id IN ({','.join('?' * len(ids))})",
                               (db.now(), *ids))
        else:
            cur = conn.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (db.now(),))
        return cur.rowcount


def start_background_checks() -> None:
    """Check every REPLY_CHECK_MINUTES while the app runs (first check one minute after start)."""
    if not config.REPLY_CHECK_AUTO:
        return

    def loop():
        time.sleep(60)
        while True:
            try:
                if ready()[0]:
                    check("auto")
                else:
                    record_due_followups()
            except Exception:
                pass
            time.sleep(config.REPLY_CHECK_MINUTES * 60)

    threading.Thread(target=loop, daemon=True, name="reply-checker").start()
