"""Draft generation, editing, approval, and rejection.

Approval safety model
---------------------
* Every draft has a content_hash = sha256(recipient + subject + body).
* Approving stores that hash in approved_hash. Only approve_drafts() writes it.
* Any edit recomputes content_hash and clears the approval, so an edited draft
  must be approved again. The sender also re-checks approved_hash ==
  content_hash immediately before sending, as a second line of defence.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from . import config, db, templates
from .domains import contact_domain
from .topics import suggest_topics
from .validation import blocking_issues, is_valid_email, normalize_email

LOCKED_SEND_STATES = ("queued", "sending", "sent", "uncertain")


class DraftError(Exception):
    pass


def content_hash(to_email: str, subject: str, body: str) -> str:
    return hashlib.sha256(f"{normalize_email(to_email)}\n{subject}\n{body}".encode("utf-8")).hexdigest()


def log_approval(conn, draft_id: int, action: str, chash: str | None, note: str = "") -> None:
    conn.execute(
        "INSERT INTO approval_log(draft_id, action, content_hash, note, created_at) VALUES (?,?,?,?,?)",
        (draft_id, action, chash, note, db.now()),
    )


def is_suppressed(conn, email: str) -> bool:
    return conn.execute("SELECT 1 FROM suppression WHERE email_norm=?", (normalize_email(email),)).fetchone() is not None


def already_contacted(conn, email: str) -> bool:
    """True if a live initial email to this address was sent or might have been sent."""
    norm = normalize_email(email)
    hit = conn.execute(
        "SELECT 1 FROM send_log WHERE recipient_norm=? AND mode='live' AND kind='initial' "
        "AND (result='sent' OR (result='uncertain' AND resolved != 'not_sent'))",
        (norm,),
    ).fetchone()
    if hit:
        return True
    return conn.execute(
        "SELECT 1 FROM drafts WHERE lower(to_email)=? AND kind='initial' AND send_status IN ('sent','uncertain','sending')",
        (norm,),
    ).fetchone() is not None


def website_already_contacted(conn, contact: dict) -> str:
    """Another address at the same website already received a live initial email.
    Returns that address (or "" if none). Prevents pitching one site twice via two inboxes."""
    domain = contact_domain(contact)
    if not domain:
        return ""
    own = normalize_email(contact.get("email", ""))
    for c in conn.execute("SELECT id, email, website_url FROM contacts WHERE id != ?", (contact.get("id") or -1,)):
        if normalize_email(c["email"]) != own and contact_domain(dict(c)) == domain and already_contacted(conn, c["email"]):
            return c["email"]
    for r in conn.execute("SELECT DISTINCT recipient_norm FROM send_log WHERE mode='live' AND kind='initial' "
                          "AND (result='sent' OR (result='uncertain' AND resolved != 'not_sent'))"):
        if r["recipient_norm"] != own and contact_domain({"email": r["recipient_norm"]}) == domain:
            return r["recipient_norm"]
    return ""


def ensure_topics(conn, contact: dict) -> dict:
    options = json.loads(contact.get("topic_options") or "[]")
    if not options:
        options = suggest_topics(contact)
        selected = contact.get("selected_topic") or contact.get("suggested_topic") or options[0]
        conn.execute("UPDATE contacts SET topic_options=?, selected_topic=?, updated_at=? WHERE id=?",
                     (json.dumps(options), selected, db.now(), contact["id"]))
        contact = dict(contact, topic_options=json.dumps(options), selected_topic=selected)
    return contact


def generate_drafts(contact_ids: list[int]) -> dict:
    """Create one draft per contact. Never sends anything."""
    if len(contact_ids) > config.MAX_BATCH_SIZE:
        raise DraftError(f"You can generate at most {config.MAX_BATCH_SIZE} drafts at a time.")
    created, skipped, warnings = [], [], []
    with db.transaction() as conn:
        settings = db.get_settings(conn)
        problem = config.sender_email_problem(settings.get("sender_email", ""))
        if problem:
            warnings.append(problem + " Drafts were created without an email address in the signature.")
        for cid in contact_ids:
            contact = db.row(conn, "SELECT * FROM contacts WHERE id=?", (cid,))
            if not contact:
                skipped.append({"contact_id": cid, "reason": "Contact not found"})
                continue
            reason = None
            problems = blocking_issues(contact)
            if problems:
                reason = "; ".join(problems)
            elif contact["outreach_status"] in ("opted_out", "do_not_contact", "replied", "not_interested", "bounced"):
                reason = f"Contact status is '{contact['outreach_status']}'"
            elif is_suppressed(conn, contact["email"]):
                reason = "Email is on the do-not-contact / suppression list"
            elif already_contacted(conn, contact["email"]):
                reason = "This address was already contacted (duplicate prevention)"
            elif website_already_contacted(conn, contact):
                reason = (f"This website was already contacted at {website_already_contacted(conn, contact)} "
                          "(duplicate prevention)")
            elif conn.execute(
                "SELECT 1 FROM drafts WHERE contact_id=? AND kind='initial' AND draft_status!='cancelled' "
                "AND approval_status!='rejected' AND send_status IN ('not_sent','queued','failed')", (cid,)
            ).fetchone():
                reason = "A draft for this contact already exists"
            if reason:
                skipped.append({"contact_id": cid, "email": contact["email"], "reason": reason})
                continue

            contact = ensure_topics(conn, contact)
            options = json.loads(contact["topic_options"])
            topic = contact["selected_topic"] or options[0]
            alt = next((t for t in options if t != topic), "")
            subject, body = templates.build_initial(contact, topic, alt, settings)
            draft_id = _insert_draft(conn, contact, "initial", None, subject, body, topic, None)
            conn.execute("UPDATE contacts SET outreach_status='drafted', updated_at=? WHERE id=? AND outreach_status='new'",
                         (db.now(), cid))
            created.append(draft_id)
    return {"created": created, "skipped": skipped, "warnings": warnings}


def _insert_draft(conn, contact, kind, parent_id, subject, body, topic, due_at) -> int:
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO drafts (contact_id, kind, parent_draft_id, to_email, website_name, subject, body, topic, "
        "content_hash, due_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (contact["id"], kind, parent_id, contact["email"].strip(), contact["website_name"], subject, body, topic,
         content_hash(contact["email"], subject, body), due_at, ts, ts),
    )
    log_approval(conn, cur.lastrowid, "created", content_hash(contact["email"], subject, body))
    return cur.lastrowid


def get_draft(conn, draft_id: int) -> dict:
    d = db.row(conn, "SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not d:
        raise DraftError("Draft not found.")
    return d


def update_draft(draft_id: int, subject: str | None = None, body: str | None = None, topic: str | None = None) -> dict:
    with db.transaction() as conn:
        d = get_draft(conn, draft_id)
        if d["send_status"] in LOCKED_SEND_STATES:
            raise DraftError(f"This draft cannot be edited because its sending status is '{d['send_status']}'.")
        new_subject = d["subject"] if subject is None else subject.strip()
        new_body = d["body"] if body is None else body.strip()
        new_topic = d["topic"] if topic is None else topic.strip()
        if not new_subject or not new_body:
            raise DraftError("Subject and body cannot be empty.")
        new_hash = content_hash(d["to_email"], new_subject, new_body)
        changed = new_hash != d["content_hash"]
        approval_status, approved_hash, approved_at = d["approval_status"], d["approved_hash"], d["approved_at"]
        if changed and d["approval_status"] == "approved":
            # Rule 6: edited-after-approval drafts need approval again.
            approval_status, approved_hash, approved_at = "pending", None, None
            log_approval(conn, draft_id, "approval_revoked_by_edit", new_hash)
        conn.execute(
            "UPDATE drafts SET subject=?, body=?, topic=?, content_hash=?, approval_status=?, approved_hash=?, "
            "approved_at=?, draft_status=CASE WHEN ? THEN 'edited' ELSE draft_status END, updated_at=? WHERE id=?",
            (new_subject, new_body, new_topic, new_hash, approval_status, approved_hash, approved_at, changed, db.now(), draft_id),
        )
        if changed:
            log_approval(conn, draft_id, "edited", new_hash)
        return get_draft(conn, draft_id)


def approve_drafts(draft_ids: list[int]) -> dict:
    approved, refused = [], []
    with db.transaction() as conn:
        for did in draft_ids:
            try:
                d = get_draft(conn, did)
            except DraftError:
                refused.append({"draft_id": did, "reason": "Draft not found"})
                continue
            reason = None
            if d["draft_status"] == "cancelled":
                reason = "Draft was cancelled"
            elif d["send_status"] in LOCKED_SEND_STATES:
                reason = f"Sending status is '{d['send_status']}'"
            elif not is_valid_email(d["to_email"]):
                reason = "Invalid recipient email"
            elif is_suppressed(conn, d["to_email"]):
                reason = "Recipient is on the suppression list"
            elif d["kind"] == "initial" and already_contacted(conn, d["to_email"]):
                reason = "Recipient was already contacted"
            if reason:
                refused.append({"draft_id": did, "reason": reason})
                continue
            conn.execute(
                "UPDATE drafts SET approval_status='approved', approved_hash=content_hash, approved_at=?, updated_at=? WHERE id=?",
                (db.now(), db.now(), did),
            )
            log_approval(conn, did, "approved", d["content_hash"])
            approved.append(did)
    return {"approved": approved, "refused": refused}


def reject_drafts(draft_ids: list[int]) -> dict:
    rejected = []
    with db.transaction() as conn:
        for did in draft_ids:
            d = db.row(conn, "SELECT * FROM drafts WHERE id=?", (did,))
            if not d or d["send_status"] in ("sending", "sent", "uncertain"):
                continue
            conn.execute(
                "UPDATE drafts SET approval_status='rejected', approved_hash=NULL, approved_at=NULL, updated_at=? WHERE id=?",
                (db.now(), did),
            )
            if d["send_status"] == "queued":
                _unqueue(conn, did, "Draft rejected before sending")
            log_approval(conn, did, "rejected", d["content_hash"])
            rejected.append(did)
    return {"rejected": rejected}


def unapprove(draft_ids: list[int]) -> None:
    with db.transaction() as conn:
        for did in draft_ids:
            d = db.row(conn, "SELECT * FROM drafts WHERE id=?", (did,))
            if d and d["approval_status"] == "approved" and d["send_status"] not in ("sending", "sent", "uncertain"):
                conn.execute("UPDATE drafts SET approval_status='pending', approved_hash=NULL, approved_at=NULL WHERE id=?", (did,))
                if d["send_status"] == "queued":
                    _unqueue(conn, did, "Approval withdrawn")
                log_approval(conn, did, "approval_withdrawn", d["content_hash"])


def _unqueue(conn, draft_id: int, note: str) -> None:
    conn.execute("UPDATE send_items SET status='cancelled', note=? WHERE draft_id=? AND status='queued'", (note, draft_id))
    conn.execute("UPDATE drafts SET send_status='not_sent' WHERE id=? AND send_status='queued'", (draft_id,))


def delete_draft(draft_id: int) -> None:
    with db.transaction() as conn:
        d = get_draft(conn, draft_id)
        if d["send_status"] in ("sending", "sent", "uncertain"):
            raise DraftError("Sent or possibly-sent drafts are kept for your records and cannot be deleted.")
        conn.execute("DELETE FROM send_items WHERE draft_id=? AND status IN ('queued','cancelled','skipped','failed')", (draft_id,))
        if conn.execute("SELECT 1 FROM send_items WHERE draft_id=?", (draft_id,)).fetchone():
            raise DraftError("This draft has sending history and cannot be deleted.")
        conn.execute("UPDATE drafts SET parent_draft_id=NULL WHERE parent_draft_id=?", (draft_id,))
        conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        log_approval(conn, draft_id, "deleted", d["content_hash"])


def cancel_pending_for_email(conn, email: str, reason: str, followups_only: bool = False) -> int:
    """Cancel unsent drafts (and queued items) for an address."""
    norm = normalize_email(email)
    kind_clause = "AND kind='followup'" if followups_only else ""
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM drafts WHERE lower(to_email)=? AND send_status IN ('not_sent','queued','failed') "
        f"AND draft_status!='cancelled' {kind_clause}", (norm,))]
    for did in ids:
        _unqueue(conn, did, reason)
        conn.execute("UPDATE drafts SET draft_status='cancelled', approval_status=CASE WHEN approval_status='approved' "
                     "THEN 'pending' ELSE approval_status END, approved_hash=NULL, updated_at=? WHERE id=?", (db.now(), did))
        log_approval(conn, did, "cancelled", None, reason)
    return len(ids)


# ---------------------------------------------------------------- follow-ups

def create_followups(parent_ids: list[int], delay_days: int) -> dict:
    created, skipped = [], []
    if delay_days < 1 or delay_days > 60:
        raise DraftError("Follow-up delay must be between 1 and 60 days.")
    with db.transaction() as conn:
        settings = db.get_settings(conn)
        if settings.get("followups_enabled") != "true":
            raise DraftError("Follow-ups are disabled. Enable them in Settings first.")
        for pid in parent_ids:
            parent = db.row(conn, "SELECT * FROM drafts WHERE id=?", (pid,))
            reason = None
            contact = None
            if not parent or parent["kind"] != "initial":
                reason = "Original email not found"
            elif parent["send_status"] != "sent":
                reason = "Original email has not been sent"
            else:
                contact = db.row(conn, "SELECT * FROM contacts WHERE id=?", (parent["contact_id"],)) if parent["contact_id"] else None
                existing = conn.execute("SELECT COUNT(*) FROM drafts WHERE parent_draft_id=? AND kind='followup' "
                                        "AND draft_status!='cancelled'", (pid,)).fetchone()[0]
                if existing >= config.MAX_FOLLOWUPS_PER_EMAIL:
                    reason = "A follow-up already exists for this email"
                elif contact is None:
                    reason = "Contact was removed"
                elif contact["outreach_status"] in ("replied", "opted_out", "do_not_contact", "not_interested", "bounced"):
                    reason = f"Contact status is '{contact['outreach_status']}'"
                elif is_suppressed(conn, parent["to_email"]):
                    reason = "Recipient is suppressed"
            if reason:
                skipped.append({"draft_id": pid, "reason": reason})
                continue
            sent_at = datetime.fromisoformat(parent["sent_at"]) if parent["sent_at"] else datetime.now(timezone.utc)
            due = (sent_at + timedelta(days=delay_days)).replace(microsecond=0).isoformat()
            subject, body = templates.build_followup(contact, parent, settings)
            fid = _insert_draft(conn, dict(contact, email=parent["to_email"]), "followup", pid, subject, body, parent["topic"], due)
            created.append(fid)
    return {"created": created, "skipped": skipped}
