"""FastAPI application: JSON API + static dashboard."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (auth, claude_agent, config, contacts, csv_sync, db, discovery, drafts, free_discovery, gmail_auth, replies, sender,
               websites)
from .importer import VALID_STATUSES, import_contacts
from .topics import NICHES
from .validation import is_valid_email

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    with db.transaction() as conn:
        problem = config.sender_email_problem(db.get_settings(conn).get("sender_email", ""))
    if problem:
        print(f"WARNING: {problem}")
    # Website discovery: runs once per local day when the app starts (never sends email).
    result = discovery.on_startup()
    print(f"Website discovery: {result['message']}")
    print(f"Claude auto-discovery: {claude_agent.on_startup()['message']}")
    replies.start_background_checks()  # reply / bounce / follow-up-due alerts; reads Gmail headers only
    yield


app = FastAPI(title="Guest Post Outreach", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(drafts.DraftError)
@app.exception_handler(sender.SendError)
@app.exception_handler(contacts.ContactError)
@app.exception_handler(websites.WebsiteError)
async def _user_error(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ------------------------------------------------------------------ models

class Ids(BaseModel):
    ids: list[int] = Field(default_factory=list)


class ContactIn(BaseModel):
    website_name: str | None = None
    website_url: str | None = None
    contact_name: str | None = None
    email: str | None = None
    niche: str | None = None
    article_url: str | None = None
    suggested_topic: str | None = None
    notes: str | None = None
    email_verified: bool | None = None
    selected_topic: str | None = None
    outreach_status: str | None = None


class StatusIn(BaseModel):
    status: str


class VerifyIn(BaseModel):
    ids: list[int]
    verified: bool = True


class TopicsIn(BaseModel):
    ids: list[int]
    regenerate: bool = False


class DraftEdit(BaseModel):
    subject: str | None = None
    body: str | None = None
    topic: str | None = None


class ConfirmIn(BaseModel):
    batch_id: int
    token: str
    confirm_text: str


class ResolveIn(BaseModel):
    resolution: str


class FollowupIn(BaseModel):
    ids: list[int]
    delay_days: int


class SuppressIn(BaseModel):
    email: str
    reason: str = "do_not_contact"


class SettingsIn(BaseModel):
    provider: str | None = None
    test_mode: bool | None = None
    test_recipient: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    sender_description: str | None = None
    sender_website: str | None = None
    sender_postal_address: str | None = None
    send_delay_seconds: int | None = None
    daily_send_limit: int | None = None
    followups_enabled: bool | None = None
    followup_delay_days: int | None = None


# ------------------------------------------------------------------ pages

@app.middleware("http")
async def _require_login(request: Request, call_next):
    """With APP_PASSWORD set, everything except the login page needs a valid session cookie."""
    from fastapi.responses import HTMLResponse, RedirectResponse
    path = request.url.path
    if auth.misconfigured() and path != "/healthz":
        if path.startswith("/api/"):
            return JSONResponse(status_code=503, content={"detail": "APP_PASSWORD is not set on the server."})
        return HTMLResponse(auth.MISCONFIGURED_PAGE, status_code=503)
    if auth.enabled() and not auth.is_open_path(path) and not auth.valid_token(request.cookies.get(auth.COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Please sign in again."})
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/login", include_in_schema=False)
def login_page(error: str = ""):
    from fastapi.responses import HTMLResponse
    msg = f'<div class="banner bad">{error}</div>' if error in ("Wrong password.", "Too many attempts. Wait 15 minutes and try again.") else ""
    return HTMLResponse(auth.LOGIN_PAGE.replace("{error}", msg))


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    from urllib.parse import quote

    from fastapi.responses import RedirectResponse
    form = await request.form()
    ok, err = auth.check_password(str(form.get("password", "")), request.client.host if request.client else "?")
    if not ok:
        import asyncio
        await asyncio.sleep(0.5)  # slows down password guessing without blocking other requests
        return RedirectResponse(f"/login?error={quote(err)}", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_token(), max_age=config.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https" or config.ON_SERVER)
    return resp


@app.get("/logout", include_in_schema=False)
def logout():
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.middleware("http")
async def _no_stale_dashboard(request: Request, call_next):
    """Make the browser re-check the dashboard files on every load, so updates show up without Ctrl+F5."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def index():
    from fastapi.responses import HTMLResponse
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):  # version each file by its modification time
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int((STATIC_DIR / name).stat().st_mtime)}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/stats")
def stats():
    with db.transaction() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        settings = db.get_settings(conn)
        active = db.row(conn, "SELECT id, status FROM send_batches WHERE status IN ('sending','awaiting_confirmation') ORDER BY id DESC LIMIT 1")
        return {
            "total_contacts": q("SELECT COUNT(*) FROM contacts"),
            "awaiting_approval": q("SELECT COUNT(*) FROM drafts WHERE approval_status='pending' AND draft_status!='cancelled' "
                                   "AND send_status IN ('not_sent','failed')"),
            "approved": q("SELECT COUNT(*) FROM drafts WHERE approval_status='approved' AND draft_status!='cancelled' "
                          "AND send_status IN ('not_sent','failed','queued')"),
            "sent": q("SELECT COUNT(*) FROM send_log WHERE mode='live' AND result='sent'"),
            "failed_or_uncertain": q("SELECT COUNT(*) FROM send_log WHERE result='failed' OR (result='uncertain' AND resolved='')"),
            "suppressed": q("SELECT COUNT(*) FROM suppression"),
            "sent_last_24h": sender.sends_last_24h(conn),
            "daily_limit": int(settings.get("daily_send_limit", "50")),
            "mode": sender.send_mode(settings),
            "provider": settings.get("provider"),
            "active_batch": active,
        }


# ------------------------------------------------------------------ contacts

@app.post("/api/import")
async def upload_contacts(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "File is larger than 5 MB.")
    try:
        return import_contacts(file.filename or "", content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Could not read the file: {type(exc).__name__}. Is it a valid CSV or .xlsx file?")


@app.get("/api/import/template.csv", include_in_schema=False)
def import_template():
    return PlainTextResponse(
        "Website Name,Website URL,Contact Name,Email,Niche,Relevant Article URL,Suggested Topic,Personalization Notes,Outreach Status,Email Verified\n"
        "Example Learning Blog,https://example.com,Jane Doe,editor@example.com,Education,https://example.com/category/online-learning,,"
        "Found email on their Write for Us page,new,yes\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contacts_template.csv"},
    )


@app.get("/api/contacts")
def get_contacts():
    return contacts.list_contacts()


@app.post("/api/contacts")
def add_contact(body: ContactIn):
    data = body.model_dump(exclude_none=True)
    return {"id": contacts.create_contact(data)}


@app.put("/api/contacts/{contact_id}")
def edit_contact(contact_id: int, body: ContactIn):
    data = body.model_dump(exclude_none=True)
    status = data.pop("outreach_status", None)
    contacts.update_contact(contact_id, data)
    if status:
        contacts.set_status(contact_id, status)
    return {"ok": True}


@app.delete("/api/contacts/{contact_id}")
def remove_contact(contact_id: int):
    return contacts.delete_contact(contact_id)


@app.post("/api/contacts/delete")
def remove_contacts(body: Ids):
    return [contacts.delete_contact(i) for i in body.ids]


@app.post("/api/contacts/{contact_id}/status")
def contact_status(contact_id: int, body: StatusIn):
    return contacts.set_status(contact_id, body.status)


@app.post("/api/contacts/verify")
def verify_contacts(body: VerifyIn):
    return {"updated": contacts.bulk_verify(body.ids, body.verified)}


@app.get("/api/meta")
def meta():
    return {"niches": NICHES, "statuses": sorted(VALID_STATUSES), "max_batch": config.MAX_BATCH_SIZE,
            "login_enabled": auth.enabled()}


@app.post("/api/topics/generate")
def topics_generate(body: TopicsIn):
    return contacts.generate_topics(body.ids, body.regenerate)


# ------------------------------------------------------------------ drafts

DRAFT_LIST_SQL = (
    "SELECT d.id, d.contact_id, d.kind, d.parent_draft_id, d.to_email, d.website_name, d.subject, d.body, d.topic, "
    "d.draft_status, d.approval_status, d.send_status, d.due_at, d.sent_at, d.approved_at, d.created_at, d.updated_at, "
    "(d.approval_status='approved' AND d.approved_hash=d.content_hash) AS approval_valid, c.notes, c.contact_name "
    "FROM drafts d LEFT JOIN contacts c ON c.id=d.contact_id "
)


@app.post("/api/drafts/generate")
def drafts_generate(body: Ids):
    return drafts.generate_drafts(body.ids)


@app.get("/api/drafts")
def drafts_list(kind: str = "initial", include_cancelled: bool = False):
    with db.transaction() as conn:
        where = "WHERE d.kind=?" + ("" if include_cancelled else " AND d.draft_status!='cancelled'")
        return db.rows(conn, DRAFT_LIST_SQL + where + " ORDER BY d.id DESC", (kind,))


@app.get("/api/drafts/{draft_id}")
def draft_get(draft_id: int):
    with db.transaction() as conn:
        d = db.row(conn, DRAFT_LIST_SQL + "WHERE d.id=?", (draft_id,))
        if not d:
            raise HTTPException(404, "Draft not found")
        d["history"] = db.rows(conn, "SELECT action, note, created_at FROM approval_log WHERE draft_id=? ORDER BY id", (draft_id,))
        return d


@app.put("/api/drafts/{draft_id}")
def draft_edit(draft_id: int, body: DraftEdit):
    d = drafts.update_draft(draft_id, body.subject, body.body, body.topic)
    return {"id": d["id"], "approval_status": d["approval_status"], "draft_status": d["draft_status"]}


@app.post("/api/drafts/approve")
def drafts_approve(body: Ids):
    return drafts.approve_drafts(body.ids)


@app.post("/api/drafts/reject")
def drafts_reject(body: Ids):
    return drafts.reject_drafts(body.ids)


@app.post("/api/drafts/unapprove")
def drafts_unapprove(body: Ids):
    drafts.unapprove(body.ids)
    return {"ok": True}


@app.delete("/api/drafts/{draft_id}")
def draft_delete(draft_id: int):
    drafts.delete_draft(draft_id)
    _close_followup_alerts([draft_id])
    return {"ok": True}


@app.post("/api/drafts/delete")
def drafts_delete_many(body: Ids):
    """Delete several unsent drafts. Sent or possibly-sent drafts are kept and reported as skipped."""
    deleted, skipped = [], []
    for i in body.ids:
        try:
            drafts.delete_draft(i)
            deleted.append(i)
        except drafts.DraftError as exc:
            skipped.append({"id": i, "reason": str(exc)})
    _close_followup_alerts(deleted)
    return {"deleted": deleted, "skipped": skipped}


def _close_followup_alerts(draft_ids: list[int]) -> None:
    """A deleted follow-up's 'due' alert no longer needs attention: mark it read, keep the record."""
    if not draft_ids:
        return
    with db.transaction() as conn:
        conn.execute(f"UPDATE notifications SET read_at=COALESCE(read_at, ?), action='Follow-up draft deleted by you' "
                     f"WHERE kind='followup_due' AND draft_id IN ({','.join('?' * len(draft_ids))})", (db.now(), *draft_ids))


# ------------------------------------------------------------------ sending

@app.post("/api/send/prepare")
def send_prepare(body: Ids):
    return sender.prepare_batch(body.ids)


@app.post("/api/send/confirm")
def send_confirm(body: ConfirmIn):
    return sender.confirm_batch(body.batch_id, body.token, body.confirm_text)


@app.post("/api/send/batches/{batch_id}/cancel")
def send_cancel(batch_id: int):
    return sender.cancel_batch(batch_id)


@app.get("/api/send/batches")
def batches_list():
    with db.transaction() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM send_batches ORDER BY id DESC LIMIT 20")]
        return [sender.batch_summary(conn, i) for i in ids]


@app.get("/api/send/batches/{batch_id}")
def batch_get(batch_id: int):
    with db.transaction() as conn:
        return {"batch": sender.batch_summary(conn, batch_id), "items": sender.batch_items(conn, batch_id)}


@app.get("/api/history")
def history():
    with db.transaction() as conn:
        return db.rows(conn, "SELECT * FROM send_log ORDER BY id DESC LIMIT 500")


@app.post("/api/history/{log_id}/resolve")
def history_resolve(log_id: int, body: ResolveIn):
    sender.resolve_uncertain(log_id, body.resolution)
    return {"ok": True}


# ------------------------------------------------------------------ follow-ups

@app.get("/api/followups")
def followups():
    with db.transaction() as conn:
        eligible = db.rows(conn,
            "SELECT d.id, d.to_email, d.website_name, d.subject, d.sent_at, c.outreach_status, "
            "(SELECT COUNT(*) FROM drafts f WHERE f.parent_draft_id=d.id AND f.kind='followup' AND f.draft_status!='cancelled') AS followups, "
            "(SELECT n.id FROM notifications n WHERE n.kind IN ('reply','bounce') AND (n.draft_id=d.id OR n.draft_id IN "
            "(SELECT f.id FROM drafts f WHERE f.parent_draft_id=d.id)) ORDER BY n.id DESC LIMIT 1) AS reply_id, "
            "(SELECT n.kind FROM notifications n WHERE n.kind IN ('reply','bounce') AND (n.draft_id=d.id OR n.draft_id IN "
            "(SELECT f.id FROM drafts f WHERE f.parent_draft_id=d.id)) ORDER BY n.id DESC LIMIT 1) AS reply_kind "
            "FROM drafts d LEFT JOIN contacts c ON c.id=d.contact_id WHERE d.kind='initial' AND d.send_status='sent' ORDER BY d.sent_at DESC")
        return {"sent_originals": eligible, "followups": db.rows(conn, DRAFT_LIST_SQL + "WHERE d.kind='followup' ORDER BY d.id DESC")}


@app.post("/api/followups/generate")
def followups_generate(body: FollowupIn):
    return drafts.create_followups(body.ids, body.delay_days)


# ------------------------------------------------------------------ suppression

@app.get("/api/suppression")
def suppression_list():
    return contacts.list_suppression()


@app.post("/api/suppression")
def suppression_add(body: SuppressIn):
    return contacts.add_suppression(body.email, body.reason)


@app.delete("/api/suppression/{email}")
def suppression_remove(email: str):
    contacts.remove_suppression(email)
    return {"ok": True}


# ------------------------------------------------------------------ settings

@app.get("/api/settings")
def settings_get():
    with db.transaction() as conn:
        s = db.get_settings(conn)
    s["gmail"] = gmail_auth.status()
    s["smtp_configured"] = bool(config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD)
    s["max_batch"] = config.MAX_BATCH_SIZE
    return s


@app.put("/api/settings")
def settings_put(body: SettingsIn):
    data = body.model_dump(exclude_none=True)
    if "provider" in data and data["provider"] not in ("dryrun", "gmail", "smtp"):
        raise HTTPException(400, "Provider must be dryrun, gmail or smtp.")
    for key in ("test_recipient", "sender_email"):
        if data.get(key) and not is_valid_email(data[key]):
            raise HTTPException(400, f"{key.replace('_', ' ').capitalize()} is not a valid email address.")
    if "send_delay_seconds" in data and not 5 <= data["send_delay_seconds"] <= 600:
        raise HTTPException(400, "Delay between emails must be between 5 and 600 seconds.")
    if "daily_send_limit" in data and not 1 <= data["daily_send_limit"] <= 2000:
        raise HTTPException(400, "Daily limit must be between 1 and 2000. Use a value at or below your provider's limit.")
    if "followup_delay_days" in data and not 1 <= data["followup_delay_days"] <= 60:
        raise HTTPException(400, "Follow-up delay must be between 1 and 60 days.")
    with db.transaction() as conn:
        if db.row(conn, "SELECT id FROM send_batches WHERE status='sending'") and ({"provider", "test_mode"} & data.keys()):
            raise HTTPException(400, "You cannot change the provider or test mode while a batch is sending.")
        for key, value in data.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value).strip()))
        if data.get("followups_enabled") is False:
            # Turning follow-ups off cancels any that haven't been sent.
            for r in db.rows(conn, "SELECT DISTINCT to_email FROM drafts WHERE kind='followup' AND send_status IN ('not_sent','failed')"):
                drafts.cancel_pending_for_email(conn, r["to_email"], "Follow-ups disabled", followups_only=True)
    return settings_get()


# ------------------------------------------------------------------ website discovery

class WebsiteIn(BaseModel):
    website_name: str | None = None
    contact_name: str | None = None
    email: str | None = None
    niche: str | None = None
    article_url: str | None = None
    guidelines_url: str | None = None
    suggested_topic: str | None = None
    personalization_notes: str | None = None
    discovery_status: str | None = None
    outreach_status: str | None = None
    accepts_guest_posts: str | None = None


class WebsiteStatusIn(BaseModel):
    ids: list[int]
    status: str


FILTER_KEYS = ("niche", "da_min", "da_max", "dr_min", "dr_max", "email_verification", "accepts", "outreach_status",
               "discovery_status", "date_from", "date_to", "missing", "q")


def _filters(request: Request) -> dict:
    return {k: request.query_params.get(k) for k in FILTER_KEYS if request.query_params.get(k)}


@app.get("/api/discovery/status")
def discovery_status():
    s = discovery.status()
    s["stats"] = websites.stats()
    return s


@app.post("/api/discovery/run")
def discovery_run():
    """'Run Website Discovery Now'. Starts in the background; discovery never sends email."""
    from .search_providers import resolve_provider
    _, method = resolve_provider()
    pending = free_discovery.queue_status(limit=0)["pending"]
    if not method["automatic"] and pending == 0:
        # Free mode doesn't search by itself; a run now would find nothing.
        return {"started": False, "nothing_to_do": True, "pending": 0,
                "message": "Nothing is waiting to be inspected. In free mode the app doesn't search the internet by itself. "
                           "Import website URLs in step 2 of the Free discovery workflow first, or ask Claude to find more websites."}
    result = discovery.run_in_background("manual", force=True)
    result["pending"] = pending
    return result


# ---- free discovery (no paid API): generated searches you run in your browser + URL import

class ImportIn(BaseModel):
    text: str = Field(default="", max_length=5_000_000)
    niche: str = ""
    query: str = ""
    process: bool = True


def _after_import(result: dict, process: bool) -> dict:
    result["run"] = (discovery.run_in_background("import", force=True) if process and result["queued"]
                     else {"started": False, "message": "No new websites to inspect." if not result["queued"]
                           else "Queued. Click Process pending imports to inspect them."})
    result["pending"] = free_discovery.queue_status(limit=0)["pending"]
    return result


@app.get("/api/discovery/queries")
def discovery_queries(niche: str = "", keywords: str = ""):
    return free_discovery.generate_queries(niche, keywords)


@app.post("/api/discovery/import")
def discovery_import(body: ImportIn):
    """Queue website URLs you collected yourself. Inspection runs in the background; nothing is sent."""
    if not body.text.strip():
        raise HTTPException(400, "Paste some URLs or search results first.")
    return _after_import(free_discovery.import_urls(body.text, body.niche, body.query, "paste"), body.process)


@app.post("/api/discovery/import-file")
async def discovery_import_file(file: UploadFile = File(...), niche: str = Form(""), query: str = Form(""),
                                process: bool = Form(True)):
    name = file.filename or "upload"
    if not name.lower().endswith((".txt", ".csv", ".html", ".htm", ".tsv", ".md")):
        raise HTTPException(400, "Upload a .txt, .csv, .tsv, .md or saved .html page.")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "File is larger than 5 MB.")
    text = content.decode("utf-8-sig", errors="replace")
    return _after_import(free_discovery.import_urls(text, niche, query, f"file:{name}"), process)


class ClaudeDiscoverIn(BaseModel):
    niches: list[str] = Field(default_factory=list)
    per_niche: int | None = None


class ClaudeSettingsIn(BaseModel):
    daily: bool


@app.get("/api/claude/status")
def claude_status():
    return claude_agent.status()


@app.post("/api/claude/discover")
def claude_discover(body: ClaudeDiscoverIn):
    """Claude searches the web and looks up free DR; the app imports and inspects. Sends nothing."""
    return claude_agent.start_in_background("button", body.niches, body.per_niche)


@app.put("/api/claude/settings")
def claude_settings(body: ClaudeSettingsIn):
    claude_agent.set_daily(body.daily)
    return claude_agent.status()


@app.get("/api/discovery/import-queue")
def discovery_import_queue(limit: int = 30):
    return free_discovery.queue_status(limit=max(1, min(limit, 200)))


@app.get("/api/discovery/websites")
def discovery_websites(request: Request, sort: str = "discovered", direction: str = "desc"):
    try:
        return websites.list_websites(_filters(request), sort, direction)
    except ValueError:
        raise HTTPException(400, "DA/DR filters must be numbers.")


@app.put("/api/discovery/websites/{website_id}")
def discovery_website_edit(website_id: int, body: WebsiteIn):
    return websites.update_website(website_id, body.model_dump(exclude_none=True))


@app.get("/api/discovery/websites/{website_id}/changes")
def discovery_website_changes(website_id: int):
    with db.transaction() as conn:
        return db.rows(conn, "SELECT field, old_value, new_value, source, created_at FROM website_changes "
                             "WHERE website_id=? ORDER BY id DESC LIMIT 100", (website_id,))


@app.post("/api/discovery/websites/status")
def discovery_websites_status(body: WebsiteStatusIn):
    return {"updated": websites.set_discovery_status(body.ids, body.status)}


@app.post("/api/discovery/websites/to-contacts")
def discovery_to_contacts(body: Ids):
    """Create unverified outreach contacts. Creates no drafts and sends nothing."""
    return websites.add_to_outreach(body.ids)


@app.get("/api/discovery/export.csv", include_in_schema=False)
def discovery_export(request: Request, scope: str = "all", sort: str = "discovered", direction: str = "asc"):
    with db.transaction() as conn:
        websites.sync_from_contacts(conn)
    rows = websites.list_websites(_filters(request) if scope == "filtered" else {}, sort, direction)
    name = "niche_websites_outreach.csv" if scope != "filtered" else "niche_websites_filtered.csv"
    return PlainTextResponse("﻿" + csv_sync.render(rows), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename={name}"})


@app.post("/api/discovery/export-file")
def discovery_export_file():
    """Rewrite output/niche_websites_outreach.csv from the database now."""
    return csv_sync.export()


# ------------------------------------------------------------------ alerts (replies, bounces, follow-ups due)

class NotificationIds(BaseModel):
    ids: list[int] = Field(default_factory=list)


@app.get("/api/notifications")
def notifications_list():
    return replies.summary()


@app.post("/api/notifications/check")
def notifications_check():
    """Look for replies and bounces now (headers only). Sends nothing."""
    result = replies.check("manual")
    result.pop("found", None)
    return result


@app.get("/api/notifications/{notification_id}/message")
def notification_message(notification_id: int):
    """The reply's text next to the email you sent. Reads Gmail only for this one message."""
    try:
        return replies.view(notification_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/notifications/read")
def notifications_read(body: NotificationIds):
    return {"marked": replies.mark_read(body.ids)}


# ------------------------------------------------------------------ data backup / move to the server

REQUIRED_TABLES = {"contacts", "drafts", "send_log", "settings", "websites"}


@app.get("/api/admin/backup.db", include_in_schema=False)
def admin_backup():
    """Download a consistent copy of the whole database (contacts, drafts, history, websites)."""
    import sqlite3
    import tempfile
    from datetime import datetime
    tmp = Path(tempfile.mkdtemp()) / "outreach.db"
    with db.transaction() as conn:
        dst = sqlite3.connect(tmp)
        conn.backup(dst)
        dst.close()
    return FileResponse(tmp, media_type="application/octet-stream",
                        filename=f"outreach-backup-{datetime.now():%Y%m%d-%H%M}.db")


@app.post("/api/admin/restore")
async def admin_restore(file: UploadFile = File(...), confirm_text: str = Form("")):
    """Replace this app's database with an uploaded one (e.g. moving from your laptop to the server).
    The current database is saved first in DATA_DIR/backups."""
    import shutil
    import sqlite3
    import tempfile
    from datetime import datetime
    if confirm_text.strip() != "REPLACE":
        raise HTTPException(400, "Type REPLACE to confirm.")
    content = await file.read()
    if not content.startswith(b"SQLite format 3\x00"):
        raise HTTPException(400, "That file is not an outreach database (.db) file.")
    with db.transaction() as conn:
        if db.row(conn, "SELECT id FROM send_batches WHERE status='sending'"):
            raise HTTPException(400, "A batch is sending right now. Try again when it has finished.")
    tmp = Path(tempfile.mkdtemp()) / "upload.db"
    tmp.write_bytes(content)
    try:
        src = sqlite3.connect(tmp)
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES <= tables:
            raise HTTPException(400, f"That database is missing tables: {', '.join(sorted(REQUIRED_TABLES - tables))}.")
        counts = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("contacts", "drafts", "websites")}
        safety = db.get_path().parent / "backups" / f"before-restore-{datetime.now():%Y%m%d-%H%M%S}.db"
        safety.parent.mkdir(parents=True, exist_ok=True)
        with db.transaction() as conn:
            dst = sqlite3.connect(safety)
            conn.backup(dst)
            dst.close()
            target = sqlite3.connect(db.get_path())
            src.backup(target)
            target.close()
        src.close()
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    db.init_db()  # adds any newer tables/columns to the restored copy
    return {"restored": True, "counts": counts, "previous_saved_as": safety.name}


@app.get("/api/gmail/status")
def gmail_status():
    return gmail_auth.status()


@app.post("/api/gmail/connect")
def gmail_connect():
    if not gmail_auth.client_secret_present():
        raise HTTPException(400, f"Put your OAuth client file at {config.GMAIL_CLIENT_SECRET_FILE} "
                                 "(or set GMAIL_CLIENT_SECRET_JSON on the server) first (see README).")
    if gmail_auth.web_mode():  # hosted: the browser goes to Google and comes back to /oauth2callback
        try:
            return {**gmail_auth.status(), "auth_url": gmail_auth.start_web_flow()}
        except Exception as exc:
            raise HTTPException(400, f"Could not start Google sign-in: {type(exc).__name__}: {exc}")
    gmail_auth.connect_in_background()
    return gmail_auth.status()


@app.get("/oauth2callback", include_in_schema=False)
def gmail_oauth_callback(state: str = "", code: str = "", error: str = ""):
    from fastapi.responses import HTMLResponse, RedirectResponse
    if error or not code:
        return HTMLResponse(f"<p>Google sign-in was cancelled or failed ({error or 'no code'}). "
                            f"<a href='/#settings'>Back to the dashboard</a></p>", status_code=400)
    try:
        gmail_auth.finish_web_flow(state, code)
    except Exception as exc:
        return HTMLResponse(f"<p>Could not connect Gmail: {type(exc).__name__}. "
                            f"<a href='/#settings'>Back to the dashboard</a> and try again.</p>", status_code=400)
    return RedirectResponse("/#followups", status_code=303)


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    gmail_auth.disconnect()
    return gmail_auth.status()
