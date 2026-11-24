"""Discovered websites: storage, merging, filters and the link to outreach contacts.

The `websites` table holds one row per normalized root domain. A website gets
an outreach contact (the existing `contacts` table) only when you click
"Add to outreach contacts". From then on, outreach status comes from the
existing contact/draft/sending records, which are never reset here.
"""
import json

from . import db
from .domains import clean_url, contact_domain, homepage
from .email_verify import STATUSES as VERIFY_STATUSES
from .email_verify import local_status
from .importer import clean_contact, insert_contact
from .validation import is_valid_email, normalize_email

DISCOVERY_STATUSES = ("New", "Qualified", "Needs Review", "Rejected")
OUTREACH_STATUSES = ("New", "Draft Generated", "Awaiting Approval", "Approved", "Sent", "Replied", "Rejected",
                     "Bounced", "Do Not Contact")
MANUAL_OUTREACH = ("New", "Rejected", "Do Not Contact")  # settable while no contact is linked

EDITABLE = ("website_name", "contact_name", "email", "niche", "article_url", "guidelines_url", "suggested_topic",
            "personalization_notes", "discovery_status", "outreach_status", "accepts_guest_posts")


class WebsiteError(Exception):
    pass


def log_change(conn, website_id: int, field: str, old, new, source: str) -> None:
    conn.execute("INSERT INTO website_changes(website_id, field, old_value, new_value, source, created_at) VALUES (?,?,?,?,?,?)",
                 (website_id, field, "" if old is None else str(old)[:1000], "" if new is None else str(new)[:1000], source, db.now()))


def insert_website(conn, data: dict) -> int:
    ts = db.now()
    row = {"website_name": "", "website_url": "", "contact_name": "", "email": "", "email_verification": "Unknown",
           "email_verification_source": "", "contact_source_url": "", "niche": "", "article_url": "", "article_title": "",
           "article_retrieved": 0, "guidelines_url": "", "accepts_guest_posts": "Unknown", "suggested_topic": "",
           "personalization_notes": "", "outreach_status": "New", "discovery_status": "New", "confidence": 0,
           "evidence": "[]", "review_reason": "", "discovery_query": "", "source_url": "", "contact_id": None,
           "crawled_at": None, "last_checked": None}
    row.update({k: v for k, v in data.items() if k in row or k == "domain"})
    row.update(discovered_at=ts, discovered_date=db.local_today(), updated_at=ts)
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO websites ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    return cur.lastrowid


def merge_website(conn, existing: dict, new: dict, source: str) -> list[str]:
    """Fill in what's missing on an existing website without overwriting what you
    (or an earlier reliable source) already recorded. Returns changed field names."""
    changes = {}
    fill_if_empty = ("website_name", "website_url", "contact_name", "email", "contact_source_url", "niche", "article_url",
                     "article_title", "guidelines_url", "suggested_topic", "personalization_notes", "source_url",
                     "discovery_query")
    for f in fill_if_empty:
        if new.get(f) and not existing.get(f):
            changes[f] = new[f]
    if "email" in changes:  # verification belongs to the address it was done for
        for f in ("email_verification", "email_verification_source"):
            if f in new:
                changes[f] = new[f]
    if "article_url" in changes:
        changes["article_retrieved"] = int(bool(new.get("article_retrieved")))
    if new.get("accepts_guest_posts") in ("Yes", "No") and new["accepts_guest_posts"] != existing.get("accepts_guest_posts"):
        changes["accepts_guest_posts"] = new["accepts_guest_posts"]
    if not existing.get("status_locked"):
        for f in ("discovery_status", "confidence", "evidence", "review_reason"):
            if f in new and new[f] != existing.get(f):
                changes[f] = new[f]
    for f in ("crawled_at", "last_checked"):
        if new.get(f):
            changes[f] = new[f]
    if not changes:
        return []
    changes["updated_at"] = db.now()
    conn.execute(f"UPDATE websites SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?", (*changes.values(), existing["id"]))
    for f, v in changes.items():
        if f not in ("updated_at", "crawled_at", "last_checked", "evidence"):
            log_change(conn, existing["id"], f, existing.get(f), v, source)
    return [f for f in changes if f != "updated_at"]


# ------------------------------------------------------------------ contacts link

def sync_from_contacts(conn) -> int:
    """Make sure every outreach contact has a website row, so the CSV is complete.
    Existing websites are linked, never duplicated. Returns the number of new rows."""
    created = 0
    linked = {r["contact_id"] for r in conn.execute("SELECT contact_id FROM websites WHERE contact_id IS NOT NULL")}
    for c in db.rows(conn, "SELECT * FROM contacts ORDER BY id"):
        if c["id"] in linked:
            continue
        domain = contact_domain(c)
        if not domain:
            continue
        w = db.row(conn, "SELECT * FROM websites WHERE domain=?", (domain,))
        if w:
            if not w["contact_id"]:
                conn.execute("UPDATE websites SET contact_id=?, updated_at=? WHERE id=?", (c["id"], db.now(), w["id"]))
                log_change(conn, w["id"], "contact_id", "", c["id"], "Linked existing outreach contact")
            continue
        status, source = local_status(c["email"])
        insert_website(conn, {
            "domain": domain, "website_name": c["website_name"], "website_url": c["website_url"] or homepage(domain),
            "contact_name": c["contact_name"], "email": c["email"], "email_verification": status,
            "email_verification_source": source, "contact_source_url": "Added by you in Outreach contacts",
            "niche": c["niche"], "article_url": c["article_url"], "suggested_topic": c["selected_topic"] or c["suggested_topic"],
            "personalization_notes": c["notes"], "discovery_status": "Qualified", "discovery_query": "(existing outreach contact)",
            "contact_id": c["id"], "last_checked": c["updated_at"],
        })
        created += 1
    return created


def outreach_label(conn, w: dict, suppressed: set) -> str:
    """Outreach status shown in the dashboard and CSV. Derived from the real contact,
    draft and sending records whenever a contact is linked."""
    if w.get("email") and normalize_email(w["email"]) in suppressed:
        return "Do Not Contact"
    c = db.row(conn, "SELECT * FROM contacts WHERE id=?", (w["contact_id"],)) if w.get("contact_id") else None
    if not c:
        return w.get("outreach_status") or "New"
    st = c["outreach_status"]
    if st in ("do_not_contact", "opted_out") or normalize_email(c["email"]) in suppressed:
        return "Do Not Contact"
    fixed = {"replied": "Replied", "not_interested": "Rejected", "bounced": "Bounced", "contacted": "Sent"}
    if st in fixed:
        return fixed[st]
    d = db.row(conn, "SELECT send_status, approval_status, approved_hash, content_hash FROM drafts WHERE contact_id=? "
                     "AND kind='initial' AND draft_status!='cancelled' ORDER BY id DESC LIMIT 1", (c["id"],))
    if d:
        if d["send_status"] == "sent":
            return "Sent"
        if d["approval_status"] == "approved" and d["approved_hash"] == d["content_hash"]:
            return "Approved"
        if d["approval_status"] == "rejected":
            return "Rejected"
        return "Awaiting Approval"
    return "Draft Generated" if st == "drafted" else (w.get("outreach_status") if w.get("outreach_status") in MANUAL_OUTREACH else "New")


def decorate(conn, w: dict, suppressed: set) -> dict:
    w["evidence"] = json.loads(w.get("evidence") or "[]")
    w["outreach_label"] = outreach_label(conn, w, suppressed)
    return w


# ------------------------------------------------------------------ listing

SORTS = {"da": "da", "dr": "dr", "niche": "niche", "discovered": "discovered_at", "name": "website_name", "confidence": "confidence"}


def list_websites(filters: dict | None = None, sort: str = "discovered", direction: str = "desc") -> list[dict]:
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    where, params = [], []
    if f.get("niche"):
        where.append("lower(niche)=lower(?)"); params.append(f["niche"])
    for col in ("da", "dr"):
        if f.get(f"{col}_min") is not None:
            where.append(f"{col} IS NOT NULL AND {col}>=?"); params.append(float(f[f"{col}_min"]))
        if f.get(f"{col}_max") is not None:
            where.append(f"{col} IS NOT NULL AND {col}<=?"); params.append(float(f[f"{col}_max"]))
    if f.get("missing") == "da":
        where.append("da IS NULL")
    elif f.get("missing") == "dr":
        where.append("dr IS NULL")
    elif f.get("missing") == "email":
        where.append("email=''")
    if f.get("email_verification"):
        where.append("email_verification=?"); params.append(f["email_verification"])
    if f.get("accepts"):
        where.append("accepts_guest_posts=?"); params.append(f["accepts"])
    if f.get("discovery_status"):
        where.append("discovery_status=?"); params.append(f["discovery_status"])
    if f.get("date_from"):
        where.append("discovered_date>=?"); params.append(f["date_from"])
    if f.get("date_to"):
        where.append("discovered_date<=?"); params.append(f["date_to"])
    if f.get("q"):
        where.append("(domain LIKE ? OR website_name LIKE ? OR email LIKE ?)"); params += [f"%{f['q']}%"] * 3
    col = SORTS.get(sort, "discovered_at")
    order = "DESC" if direction != "asc" else "ASC"
    sql = "SELECT * FROM websites" + (" WHERE " + " AND ".join(where) if where else "")
    sql += f" ORDER BY ({col} IS NULL), {col} {order}, id DESC"
    with db.transaction() as conn:
        suppressed = {r["email_norm"] for r in conn.execute("SELECT email_norm FROM suppression")}
        out = [decorate(conn, w, suppressed) for w in db.rows(conn, sql, params)]
    if f.get("outreach_status"):
        out = [w for w in out if w["outreach_label"] == f["outreach_status"]]
    return out


def get_website(conn, website_id: int) -> dict:
    w = db.row(conn, "SELECT * FROM websites WHERE id=?", (website_id,))
    if not w:
        raise WebsiteError("Website not found.")
    return w


# ------------------------------------------------------------------ edits

def update_website(website_id: int, data: dict) -> dict:
    from .contacts import add_suppression  # local import: contacts imports drafts

    suppress_email = None
    with db.transaction() as conn:
        w = get_website(conn, website_id)
        updates = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items() if k in EDITABLE and v is not None}
        if "discovery_status" in updates:
            if updates["discovery_status"] not in DISCOVERY_STATUSES:
                raise WebsiteError("Unknown discovery status.")
            updates["status_locked"] = 1  # automation won't change a status you set
        if "outreach_status" in updates:
            if updates["outreach_status"] not in MANUAL_OUTREACH:
                raise WebsiteError(f"Outreach status can be set to {', '.join(MANUAL_OUTREACH)} here; the rest come from drafts and sending.")
            if w["contact_id"] and updates["outreach_status"] != "Do Not Contact":
                raise WebsiteError("This website has an outreach contact. Change its status in Manage contacts.")
            if updates["outreach_status"] == "Do Not Contact" and (updates.get("email") or w["email"]):
                suppress_email = updates.get("email") or w["email"]
        if "accepts_guest_posts" in updates and updates["accepts_guest_posts"] not in ("Yes", "No", "Unknown"):
            raise WebsiteError("Guest post acceptance must be Yes, No or Unknown.")
        if "email" in updates and updates["email"] != w["email"]:
            if w["contact_id"]:
                raise WebsiteError("This website is linked to an outreach contact; change the email in Manage contacts.")
            if updates["email"] and not is_valid_email(updates["email"]):
                raise WebsiteError("That email address is not valid.")
            status, source = local_status(updates["email"])
            updates.update(email=updates["email"].lower(), email_verification=status, email_verification_source=source,
                           contact_source_url="Entered by you")
        for k in ("article_url", "guidelines_url"):
            if updates.get(k):
                updates[k] = clean_url(updates[k])
                if k == "article_url":
                    updates["article_retrieved"] = 0
        if updates:
            updates["updated_at"] = db.now()
            conn.execute(f"UPDATE websites SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?", (*updates.values(), website_id))
            for k, v in updates.items():
                if k not in ("updated_at", "status_locked"):
                    log_change(conn, website_id, k, w.get(k), v, "Edited by you")
    if suppress_email:
        add_suppression(suppress_email, "do_not_contact")
    with db.transaction() as conn:
        return get_website(conn, website_id)


def set_discovery_status(ids: list[int], status: str) -> int:
    if status not in DISCOVERY_STATUSES:
        raise WebsiteError("Unknown discovery status.")
    n = 0
    with db.transaction() as conn:
        for i in ids:
            w = db.row(conn, "SELECT * FROM websites WHERE id=?", (i,))
            if w and w["discovery_status"] != status:
                conn.execute("UPDATE websites SET discovery_status=?, status_locked=1, updated_at=? WHERE id=?", (status, db.now(), i))
                log_change(conn, i, "discovery_status", w["discovery_status"], status, "Edited by you")
                n += 1
    return n


def add_to_outreach(ids: list[int]) -> dict:
    """Create (or link) an outreach contact for each website. Sends nothing and
    creates no drafts. New contacts start *unverified*, so they cannot be sent
    to until you confirm the address source in Manage contacts."""
    created, linked, skipped = [], [], []
    with db.transaction() as conn:
        suppressed = {r["email_norm"] for r in conn.execute("SELECT email_norm FROM suppression")}
        for i in ids:
            w = db.row(conn, "SELECT * FROM websites WHERE id=?", (i,))
            reason = None
            if not w:
                reason = "Website not found"
            elif w["contact_id"]:
                reason = "Already in outreach contacts"
            elif not w["email"]:
                reason = "No contact email"
            elif not is_valid_email(w["email"]):
                reason = "Email is not valid"
            elif w["discovery_status"] == "Rejected":
                reason = "Discovery status is Rejected"
            elif w["email_verification"] == "Invalid":
                reason = "Verification service reported the email as invalid"
            elif w["outreach_status"] == "Do Not Contact" or normalize_email(w["email"]) in suppressed:
                reason = "On the do-not-contact list"
            if reason:
                skipped.append({"website_id": i, "domain": w["domain"] if w else "", "reason": reason})
                continue
            existing = db.row(conn, "SELECT id FROM contacts WHERE email_norm=?", (normalize_email(w["email"]),))
            if existing:
                conn.execute("UPDATE websites SET contact_id=?, updated_at=? WHERE id=?", (existing["id"], db.now(), i))
                log_change(conn, i, "contact_id", "", existing["id"], "Linked to existing outreach contact")
                linked.append(i)
                continue
            notes = w["personalization_notes"]
            flag = f"Email check: {w['email_verification']}" + (f" ({w['email_verification_source']})" if w["email_verification_source"] else "")
            source = f"Contact source: {w['contact_source_url']}" if w["contact_source_url"] else "Contact source: unknown"
            c = clean_contact({
                "website_name": w["website_name"] or w["domain"], "website_url": w["website_url"] or homepage(w["domain"]),
                "contact_name": w["contact_name"], "email": w["email"], "niche": w["niche"], "article_url": w["article_url"],
                "suggested_topic": w["suggested_topic"], "notes": "\n".join(x for x in (notes, source, flag) if x),
                "outreach_status": "new", "email_verified": False,
            })
            cid = insert_contact(conn, c)
            conn.execute("UPDATE websites SET contact_id=?, updated_at=? WHERE id=?", (cid, db.now(), i))
            log_change(conn, i, "contact_id", "", cid, "Added to outreach contacts")
            created.append(cid)
    return {"created": created, "linked": linked, "skipped": skipped}


def stats() -> dict:
    today = db.local_today()
    with db.transaction() as conn:
        q = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]  # noqa: E731
        last = db.row(conn, "SELECT * FROM discovery_runs WHERE status='completed' ORDER BY id DESC LIMIT 1")
        summary = json.loads(last["summary"]) if last else {}
        return {
            "total": q("SELECT COUNT(*) FROM websites"),
            "new_today": q("SELECT COUNT(*) FROM websites WHERE discovered_date=? AND discovery_query NOT LIKE '(%'", (today,)),
            "qualified": q("SELECT COUNT(*) FROM websites WHERE discovery_status='Qualified'"),
            "needs_review": q("SELECT COUNT(*) FROM websites WHERE discovery_status IN ('Needs Review','New')"),
            "rejected": q("SELECT COUNT(*) FROM websites WHERE discovery_status='Rejected'"),
            "missing_email": q("SELECT COUNT(*) FROM websites WHERE email='' AND discovery_status!='Rejected'"),
            "verified_email": q("SELECT COUNT(*) FROM websites WHERE email_verification='Verified'"),
            "missing_da": q("SELECT COUNT(*) FROM websites WHERE da IS NULL AND discovery_status!='Rejected'"),
            "missing_dr": q("SELECT COUNT(*) FROM websites WHERE dr IS NULL AND discovery_status!='Rejected'"),
            "duplicates_skipped_last_run": summary.get("existing_websites", 0) + summary.get("duplicate_results", 0),
            "drafts_awaiting_approval": q("SELECT COUNT(*) FROM drafts WHERE approval_status='pending' AND draft_status!='cancelled' "
                                          "AND send_status IN ('not_sent','failed')"),
            "last_successful_run": last["finished_at"] if last else None,
            "verify_statuses": list(VERIFY_STATUSES),
        }
