"""Contact management, recipient preferences, and suppression."""
import json

from . import db
from .drafts import cancel_pending_for_email
from .importer import VALID_STATUSES, clean_contact, insert_contact
from .topics import niche_label, suggest_topics
from .validation import contact_issues, normalize_email

EDITABLE = ("website_name", "website_url", "contact_name", "email", "niche", "article_url",
            "suggested_topic", "notes", "email_verified", "selected_topic")


class ContactError(Exception):
    pass


def _decorate(conn, c: dict, suppressed: set) -> dict:
    c["topic_options"] = json.loads(c.get("topic_options") or "[]")
    c["issues"] = contact_issues(c)
    c["suppressed"] = c["email_norm"] in suppressed
    c["niche_label"] = niche_label(c["niche"]) if c["niche"] else ""
    return c


def list_contacts() -> list[dict]:
    with db.transaction() as conn:
        suppressed = {r["email_norm"] for r in conn.execute("SELECT email_norm FROM suppression")}
        result = []
        for c in db.rows(conn, "SELECT * FROM contacts ORDER BY id DESC"):
            c = _decorate(conn, c, suppressed)
            c["active_draft_id"] = (conn.execute(
                "SELECT id FROM drafts WHERE contact_id=? AND draft_status!='cancelled' ORDER BY id DESC LIMIT 1",
                (c["id"],)).fetchone() or [None])[0]
            result.append(c)
        return result


def get_contact(conn, contact_id: int) -> dict:
    c = db.row(conn, "SELECT * FROM contacts WHERE id=?", (contact_id,))
    if not c:
        raise ContactError("Contact not found.")
    return c


def create_contact(data: dict) -> int:
    c = clean_contact(data)
    norm = normalize_email(c["email"])
    if not norm:
        raise ContactError("Email is required.")
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM contacts WHERE email_norm=?", (norm,)).fetchone():
            raise ContactError("A contact with this email already exists.")
        if conn.execute("SELECT 1 FROM suppression WHERE email_norm=?", (norm,)).fetchone():
            c["outreach_status"] = "do_not_contact"
        return insert_contact(conn, c)


def update_contact(contact_id: int, data: dict) -> dict:
    with db.transaction() as conn:
        current = get_contact(conn, contact_id)
        updates = {k: v for k, v in data.items() if k in EDITABLE and v is not None}
        if "email" in updates:
            updates["email"] = str(updates["email"]).strip()
            norm = normalize_email(updates["email"])
            if not norm:
                raise ContactError("Email is required.")
            if norm != current["email_norm"]:
                if conn.execute("SELECT 1 FROM contacts WHERE email_norm=? AND id!=?", (norm, contact_id)).fetchone():
                    raise ContactError("Another contact already uses this email.")
                updates["email_norm"] = norm
                # Drafts addressed to the old email must not be sent.
                cancel_pending_for_email(conn, current["email"], "Contact email changed")
                updates["email_verified"] = int(bool(data.get("email_verified", 0)))
        if "email_verified" in updates:
            updates["email_verified"] = 1 if updates["email_verified"] in (1, True, "1", "true", "yes") else 0
        if "website_url" in updates and updates["website_url"] and not updates["website_url"].startswith(("http://", "https://")):
            updates["website_url"] = "https://" + updates["website_url"].strip()
        if ("niche" in updates and updates["niche"] != current["niche"]) or \
                ("article_url" in updates and updates["article_url"] != current["article_url"]):
            updates["topic_options"] = "[]"  # regenerate next time
        if updates:
            updates["updated_at"] = db.now()
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE contacts SET {cols} WHERE id=?", (*updates.values(), contact_id))
        return get_contact(conn, contact_id)


def delete_contact(contact_id: int) -> dict:
    """Remove a contact and all of its unsent drafts. Sent history is kept."""
    with db.transaction() as conn:
        c = get_contact(conn, contact_id)
        cancelled = cancel_pending_for_email(conn, c["email"], "Contact removed")
        pending = [r["id"] for r in conn.execute(
            "SELECT id FROM drafts WHERE contact_id=? AND send_status NOT IN ('sent','uncertain','sending')", (contact_id,))]
        for did in pending:
            if not conn.execute("SELECT 1 FROM send_items WHERE draft_id=? AND status NOT IN ('cancelled','skipped')", (did,)).fetchone():
                conn.execute("DELETE FROM send_items WHERE draft_id=?", (did,))
                conn.execute("UPDATE drafts SET parent_draft_id=NULL WHERE parent_draft_id=?", (did,))
                conn.execute("DELETE FROM drafts WHERE id=?", (did,))
        conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        return {"deleted": contact_id, "drafts_cancelled": cancelled, "drafts_removed": len(pending)}


def set_status(contact_id: int, status: str) -> dict:
    if status not in VALID_STATUSES:
        raise ContactError(f"Unknown status '{status}'.")
    with db.transaction() as conn:
        c = get_contact(conn, contact_id)
        conn.execute("UPDATE contacts SET outreach_status=?, updated_at=? WHERE id=?", (status, db.now(), contact_id))
        cancelled = 0
        if status in ("opted_out", "do_not_contact"):
            _suppress(conn, c["email"], status)
            cancelled = cancel_pending_for_email(conn, c["email"], f"Contact marked {status}")
        elif status in ("replied", "not_interested", "bounced"):
            cancelled = cancel_pending_for_email(conn, c["email"], f"Contact marked {status}", followups_only=(status == "replied"))
        return {"status": status, "drafts_cancelled": cancelled}


def _suppress(conn, email: str, reason: str) -> None:
    conn.execute("INSERT OR IGNORE INTO suppression(email_norm, reason, created_at) VALUES (?,?,?)",
                 (normalize_email(email), reason, db.now()))


def add_suppression(email: str, reason: str = "do_not_contact") -> dict:
    norm = normalize_email(email)
    if not norm:
        raise ContactError("Email is required.")
    with db.transaction() as conn:
        _suppress(conn, norm, reason or "do_not_contact")
        conn.execute("UPDATE contacts SET outreach_status='do_not_contact', updated_at=? WHERE email_norm=? "
                     "AND outreach_status NOT IN ('opted_out')", (db.now(), norm))
        cancelled = cancel_pending_for_email(conn, norm, "Added to suppression list")
    return {"email": norm, "drafts_cancelled": cancelled}


def remove_suppression(email: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM suppression WHERE email_norm=?", (normalize_email(email),))


def list_suppression() -> list[dict]:
    with db.transaction() as conn:
        return db.rows(conn, "SELECT * FROM suppression ORDER BY created_at DESC")


def bulk_verify(contact_ids: list[int], verified: bool) -> int:
    with db.transaction() as conn:
        for cid in contact_ids:
            conn.execute("UPDATE contacts SET email_verified=?, updated_at=? WHERE id=?", (1 if verified else 0, db.now(), cid))
    return len(contact_ids)


def generate_topics(contact_ids: list[int], regenerate: bool = False) -> list[dict]:
    out = []
    with db.transaction() as conn:
        for cid in contact_ids:
            c = get_contact(conn, cid)
            options = json.loads(c["topic_options"] or "[]")
            if regenerate or not options:
                options = suggest_topics(c)
                if regenerate and options == json.loads(c["topic_options"] or "[]"):
                    # Same seed gives same picks; shift the seed for a fresh set.
                    options = suggest_topics(dict(c, email=c["email"] + "#" + db.now()))
            selected = c["selected_topic"] or c["suggested_topic"] or options[0]
            conn.execute("UPDATE contacts SET topic_options=?, selected_topic=?, updated_at=? WHERE id=?",
                         (json.dumps(options), selected, db.now(), cid))
            out.append({"contact_id": cid, "topic_options": options, "selected_topic": selected})
    return out
