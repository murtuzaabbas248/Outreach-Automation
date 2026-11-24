"""Sending queue.

Flow:
  1. prepare_batch(draft_ids)   -> validates, creates batch 'awaiting_confirmation'
                                   with a one-time token. Nothing is sent.
  2. confirm_batch(id, token, "SEND") -> re-validates, starts the worker.
  3. run_batch(id)              -> for each item, re-validates immediately before
                                   sending, sends one message, records the result.

Every gate checks the database, not the browser, so calling the API directly
cannot skip approval.
"""
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, db, providers
from .drafts import already_contacted, content_hash, is_suppressed, website_already_contacted
from .validation import is_valid_email, normalize_email

SYNC = False  # tests set this to True to run the worker inline
_worker_lock = threading.Lock()
BLOCKED_CONTACT_STATUSES = ("opted_out", "do_not_contact", "bounced", "not_interested")


class SendError(Exception):
    pass


def send_mode(settings: dict) -> str:
    if settings.get("provider", "dryrun") == "dryrun":
        return "dry_run"
    return "test" if settings.get("test_mode", "true") == "true" else "live"


def sends_last_24h(conn) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM send_log WHERE mode IN ('live','test') AND result IN ('sent','uncertain') AND created_at >= ?",
        (since,),
    ).fetchone()[0]


def draft_blockers(conn, draft: dict | None, settings: dict, snapshot_hash: str | None = None) -> list[str]:
    """Every reason this draft must not be sent right now. Empty list = OK."""
    if not draft:
        return ["Draft no longer exists"]
    reasons = []
    actual_hash = content_hash(draft["to_email"], draft["subject"], draft["body"])
    if draft["draft_status"] == "cancelled":
        reasons.append("Draft was cancelled")
    if draft["approval_status"] != "approved":
        reasons.append(f"Draft is not approved (status: {draft['approval_status']})")
    elif not draft["approved_hash"] or draft["approved_hash"] != actual_hash or draft["content_hash"] != actual_hash:
        reasons.append("Draft changed after approval; it must be approved again")
    if snapshot_hash is not None and snapshot_hash != actual_hash:
        reasons.append("Draft changed after the batch was prepared")
    if draft["send_status"] in ("sending", "sent", "uncertain"):
        reasons.append(f"Already {draft['send_status']}")
    if not is_valid_email(draft["to_email"]):
        reasons.append("Invalid recipient email")
    if send_mode(settings) in ("test", "live") and config.sender_email_problem(settings.get("sender_email", "")):
        reasons.append(config.sender_email_problem(settings.get("sender_email", "")))
    if any(p in draft["body"].lower() for p in config.PLACEHOLDER_EMAILS):
        reasons.append("Draft body contains a placeholder email address; delete it and generate it again")
    if is_suppressed(conn, draft["to_email"]):
        reasons.append("Recipient is on the suppression / do-not-contact list")

    contact = db.row(conn, "SELECT * FROM contacts WHERE id=?", (draft["contact_id"],)) if draft["contact_id"] else None
    if contact is None:
        reasons.append("Contact was removed")
    else:
        if normalize_email(contact["email"]) != normalize_email(draft["to_email"]):
            reasons.append("Contact email changed since the draft was created")
        if not contact["email_verified"]:
            reasons.append("Email source is not marked as verified")
        if contact["outreach_status"] in BLOCKED_CONTACT_STATUSES:
            reasons.append(f"Contact status is '{contact['outreach_status']}'")

    if draft["kind"] == "initial":
        if send_mode(settings) == "live" and already_contacted(conn, draft["to_email"]):
            reasons.append("This address was already contacted (duplicate prevention)")
        if send_mode(settings) == "live" and contact is not None:
            other = website_already_contacted(conn, contact)
            if other:
                reasons.append(f"This website was already contacted at {other} (duplicate prevention)")
    else:
        if settings.get("followups_enabled") != "true":
            reasons.append("Follow-ups are disabled in Settings")
        parent = db.row(conn, "SELECT * FROM drafts WHERE id=?", (draft["parent_draft_id"],)) if draft["parent_draft_id"] else None
        if not parent or parent["send_status"] != "sent":
            reasons.append("Original email was not sent")
        if contact is not None and contact["outreach_status"] == "replied":
            reasons.append("Recipient has replied")
        if draft["due_at"] and draft["due_at"] > db.now():
            reasons.append(f"Follow-up is not due until {draft['due_at'][:10]}")
        if conn.execute("SELECT 1 FROM send_log WHERE draft_id=? AND mode='live' AND result IN ('sent','uncertain')",
                        (draft["id"],)).fetchone():
            reasons.append("This follow-up was already sent")
    return reasons


def _check_mode_ready(settings: dict) -> None:
    mode = send_mode(settings)
    if mode == "test" and not is_valid_email(settings.get("test_recipient", "")):
        raise SendError("Test mode is on but no valid test recipient (your own address) is set in Settings.")
    if mode in ("test", "live"):
        try:
            providers.get_provider(settings["provider"]).check_ready()
        except providers.ProviderError as exc:
            raise SendError(str(exc)) from exc


def prepare_batch(draft_ids: list[int]) -> dict:
    """Create a batch awaiting confirmation. Sends nothing."""
    draft_ids = list(dict.fromkeys(int(i) for i in draft_ids))
    if not draft_ids:
        raise SendError("Select at least one approved draft.")
    if len(draft_ids) > config.MAX_BATCH_SIZE:
        raise SendError(f"A batch can contain at most {config.MAX_BATCH_SIZE} emails.")
    with db.transaction() as conn:
        settings = db.get_settings(conn)
        _check_mode_ready(settings)
        if db.row(conn, "SELECT id FROM send_batches WHERE status='sending'"):
            raise SendError("Another batch is currently sending. Wait for it to finish or cancel it.")
        # Older unconfirmed batches are superseded.
        conn.execute("UPDATE send_items SET status='cancelled', note='Superseded by a newer batch' WHERE status='queued' "
                     "AND batch_id IN (SELECT id FROM send_batches WHERE status='awaiting_confirmation')")
        conn.execute("UPDATE send_batches SET status='cancelled', finished_at=?, stop_reason='Superseded by a newer batch' "
                     "WHERE status='awaiting_confirmation'", (db.now(),))

        eligible, excluded, seen = [], [], set()
        for did in draft_ids:
            d = db.row(conn, "SELECT * FROM drafts WHERE id=?", (did,))
            reasons = draft_blockers(conn, d, settings)
            if d and normalize_email(d["to_email"]) in seen:
                reasons.append("Duplicate recipient within this batch")
            if reasons:
                excluded.append({"draft_id": did, "to_email": d["to_email"] if d else "", "reasons": reasons})
                continue
            seen.add(normalize_email(d["to_email"]))
            eligible.append(d)

        if not eligible:
            return {"batch": None, "items": [], "excluded": excluded}

        mode = send_mode(settings)
        if mode in ("live", "test"):
            limit = int(settings.get("daily_send_limit", "50"))
            used = sends_last_24h(conn)
            if used + len(eligible) > limit:
                raise SendError(f"Daily limit: {used} of {limit} emails sent in the last 24 hours, so at most "
                                f"{max(limit - used, 0)} more can be sent now.")

        token = secrets.token_urlsafe(24)
        cur = conn.execute("INSERT INTO send_batches(status, token, mode, provider, created_at) VALUES "
                           "('awaiting_confirmation', ?, ?, ?, ?)", (token, mode, settings["provider"], db.now()))
        batch_id = cur.lastrowid
        for d in eligible:
            conn.execute("INSERT INTO send_items(batch_id, draft_id, approved_hash) VALUES (?,?,?)",
                         (batch_id, d["id"], d["approved_hash"]))
        return {"batch": batch_summary(conn, batch_id, include_token=True), "items": batch_items(conn, batch_id), "excluded": excluded}


def batch_summary(conn, batch_id: int, include_token: bool = False) -> dict:
    b = db.row(conn, "SELECT * FROM send_batches WHERE id=?", (batch_id,))
    if not b:
        raise SendError("Batch not found.")
    settings = db.get_settings(conn)
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM send_items WHERE batch_id=? GROUP BY status", (batch_id,))}
    b["counts"] = counts
    b["total"] = sum(counts.values())
    b["test_recipient"] = settings.get("test_recipient", "") if b["mode"] == "test" else ""
    b["delay_seconds"] = int(settings.get("send_delay_seconds", "45"))
    if not include_token:
        b.pop("token", None)
    return b


def batch_items(conn, batch_id: int) -> list[dict]:
    return db.rows(conn, "SELECT i.id, i.draft_id, i.status, i.note, i.attempted_at, d.to_email, d.subject, d.body, "
                         "d.website_name, d.kind, d.topic FROM send_items i JOIN drafts d ON d.id=i.draft_id "
                         "WHERE i.batch_id=? ORDER BY i.id", (batch_id,))


def confirm_batch(batch_id: int, token: str, confirm_text: str) -> dict:
    if (confirm_text or "").strip() != "SEND":
        raise SendError("Type SEND (in capital letters) to confirm.")
    with db.transaction() as conn:
        b = db.row(conn, "SELECT * FROM send_batches WHERE id=?", (batch_id,))
        if not b:
            raise SendError("Batch not found.")
        if b["status"] != "awaiting_confirmation":
            raise SendError(f"This batch cannot be confirmed (status: {b['status']}).")
        if not secrets.compare_digest(b["token"], token or ""):
            raise SendError("Invalid confirmation token. Prepare the batch again from the Sending Queue page.")
        settings = db.get_settings(conn)
        _check_mode_ready(settings)
        if send_mode(settings) != b["mode"] or settings["provider"] != b["provider"]:
            raise SendError("Sending settings changed after this batch was prepared. Prepare it again.")
        problems = []
        for item in db.rows(conn, "SELECT * FROM send_items WHERE batch_id=? AND status='queued'", (batch_id,)):
            d = db.row(conn, "SELECT * FROM drafts WHERE id=?", (item["draft_id"],))
            reasons = draft_blockers(conn, d, settings, item["approved_hash"])
            if reasons:
                problems.append({"draft_id": item["draft_id"], "reasons": reasons})
        if problems:
            _cancel_batch(conn, batch_id, "Drafts changed before confirmation; prepare the batch again.")
            raise SendError("Some drafts changed after the batch was prepared, so the batch was cancelled. "
                            "Review them and prepare a new batch. " +
                            "; ".join(f"#{p['draft_id']}: {', '.join(p['reasons'])}" for p in problems))
        conn.execute("UPDATE send_batches SET status='sending', confirmed_at=?, token='' WHERE id=?", (db.now(), batch_id))
        conn.execute("UPDATE drafts SET send_status='queued' WHERE id IN (SELECT draft_id FROM send_items WHERE batch_id=? "
                     "AND status='queued')", (batch_id,))
    start_worker(batch_id)
    with db.transaction() as conn:
        return batch_summary(conn, batch_id)


def start_worker(batch_id: int) -> None:
    if SYNC:
        run_batch(batch_id)
        return
    threading.Thread(target=run_batch, args=(batch_id,), daemon=True, name=f"send-batch-{batch_id}").start()


def _cancel_batch(conn, batch_id: int, reason: str, status: str = "cancelled") -> None:
    conn.execute("UPDATE drafts SET send_status='not_sent' WHERE send_status='queued' AND id IN "
                 "(SELECT draft_id FROM send_items WHERE batch_id=? AND status='queued')", (batch_id,))
    conn.execute("UPDATE send_items SET status='cancelled', note=? WHERE batch_id=? AND status='queued'", (reason, batch_id))
    conn.execute("UPDATE send_batches SET status=?, finished_at=?, stop_reason=? WHERE id=?", (status, db.now(), reason, batch_id))


def cancel_batch(batch_id: int) -> dict:
    with db.transaction() as conn:
        b = db.row(conn, "SELECT * FROM send_batches WHERE id=?", (batch_id,))
        if not b:
            raise SendError("Batch not found.")
        if b["status"] not in ("awaiting_confirmation", "sending"):
            raise SendError(f"Batch is already {b['status']}.")
        _cancel_batch(conn, batch_id, "Cancelled by you before sending")
        return batch_summary(conn, batch_id)


def _log(conn, batch, draft, actual_to, subject, result, response) -> None:
    conn.execute(
        "INSERT INTO send_log(batch_id, draft_id, recipient, recipient_norm, actual_recipient, subject, kind, mode, provider, "
        "result, provider_response, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch["id"], draft["id"], draft["to_email"], normalize_email(draft["to_email"]), actual_to, subject, draft["kind"],
         batch["mode"], batch["provider"], result, (response or "")[:500], db.now()),
    )


def run_batch(batch_id: int) -> None:
    """Send the queued items of a confirmed batch, one message at a time."""
    if not _worker_lock.acquire(blocking=False):
        with db.transaction() as conn:
            _cancel_batch(conn, batch_id, "Another batch was already sending", status="stopped")
        return
    try:
        _run_batch(batch_id)
    finally:
        _worker_lock.release()


def _run_batch(batch_id: int) -> None:
    with db.transaction() as conn:
        batch = db.row(conn, "SELECT * FROM send_batches WHERE id=?", (batch_id,))
        if not batch or batch["status"] != "sending":
            return
        settings = db.get_settings(conn)
    provider = providers.get_provider(batch["provider"]) if batch["mode"] != "dry_run" else providers.DryRunProvider()
    delay = max(0, int(settings.get("send_delay_seconds", "45")))
    first = True

    while True:
        # Wait between messages before picking the next one, so cancelling
        # during the pause leaves nothing half-started.
        if not first and delay and _wait_or_cancelled(batch_id, delay):
            return
        first = False
        with db.transaction() as conn:
            batch = db.row(conn, "SELECT * FROM send_batches WHERE id=?", (batch_id,))
            if batch["status"] != "sending":
                return  # cancelled
            item = db.row(conn, "SELECT * FROM send_items WHERE batch_id=? AND status='queued' ORDER BY id LIMIT 1", (batch_id,))
            if item is None:
                conn.execute("UPDATE send_batches SET status='completed', finished_at=? WHERE id=?", (db.now(), batch_id))
                return
            settings = db.get_settings(conn)
            draft = db.row(conn, "SELECT * FROM drafts WHERE id=?", (item["draft_id"],))
            # Final gate, checked under the same lock that guards edits/approvals.
            reasons = draft_blockers(conn, draft, settings, item["approved_hash"])
            if send_mode(settings) != batch["mode"]:
                reasons.append("Sending settings changed during the batch")
            if reasons:
                conn.execute("UPDATE send_items SET status='skipped', note=? WHERE id=?", ("; ".join(reasons), item["id"]))
                if draft and draft["send_status"] == "queued":
                    conn.execute("UPDATE drafts SET send_status='not_sent' WHERE id=?", (draft["id"],))
                first = True  # nothing was sent, so no need to wait before the next one
                continue
            if batch["mode"] in ("live", "test") and sends_last_24h(conn) >= int(settings.get("daily_send_limit", "50")):
                _cancel_batch(conn, batch_id, "Daily sending limit reached; remaining emails were not sent.", status="stopped")
                return
            conn.execute("UPDATE send_items SET status='sending', attempted_at=? WHERE id=?", (db.now(), item["id"]))
            conn.execute("UPDATE drafts SET send_status='sending' WHERE id=?", (draft["id"],))

        subject, body, to = draft["subject"], draft["body"], draft["to_email"]
        if batch["mode"] == "test":
            to = settings["test_recipient"]
            subject = f"[TEST] {subject}"
            body = f"--- TEST MODE: in live mode this email would go to {draft['to_email']} ---\n\n{body}"
        msg = providers.build_message(settings.get("sender_name", ""), settings.get("sender_email", ""), to, subject, body)

        outcome, response, stop_reason = "sent", "", ""
        try:
            response = provider.send(msg)
        except providers.SendRejected as exc:
            outcome, response = "failed", str(exc)
        except (providers.AuthExpired, providers.LimitReached) as exc:
            outcome, response, stop_reason = "not_sent", str(exc), str(exc)
        except providers.SendUncertain as exc:
            outcome, response, stop_reason = "uncertain", str(exc), "Delivery status uncertain; queue stopped for your review."
        except Exception as exc:  # unknown failure mid-send: we cannot be sure it wasn't sent
            outcome, response = "uncertain", f"Unexpected error: {type(exc).__name__}"
            stop_reason = "Unexpected error; queue stopped for your review."

        with db.transaction() as conn:
            live = batch["mode"] == "live"
            if outcome == "sent":
                log_result = "sent" if batch["mode"] != "dry_run" else "simulated"
                conn.execute("UPDATE send_items SET status='sent', note=? WHERE id=?",
                             ({"live": "Sent", "test": f"Test copy sent to {to}", "dry_run": "Dry run: nothing was sent"}[batch["mode"]], item["id"]))
                if live:
                    conn.execute("UPDATE drafts SET send_status='sent', sent_at=?, provider_message_id=? WHERE id=?",
                                 (db.now(), response, draft["id"]))
                    if draft["contact_id"]:
                        conn.execute("UPDATE contacts SET outreach_status='contacted', updated_at=? WHERE id=? "
                                     "AND outreach_status IN ('new','drafted')", (db.now(), draft["contact_id"]))
                else:
                    conn.execute("UPDATE drafts SET send_status='not_sent' WHERE id=?", (draft["id"],))
                _log(conn, batch, draft, to, subject, log_result, response)
            elif outcome == "failed":
                conn.execute("UPDATE send_items SET status='failed', note=? WHERE id=?", (response, item["id"]))
                conn.execute("UPDATE drafts SET send_status='failed' WHERE id=?", (draft["id"],))
                _log(conn, batch, draft, to, subject, "failed", response)
            elif outcome == "not_sent":
                conn.execute("UPDATE send_items SET status='cancelled', note=? WHERE id=?", (f"Not sent: {response}", item["id"]))
                conn.execute("UPDATE drafts SET send_status='not_sent' WHERE id=?", (draft["id"],))
                _log(conn, batch, draft, to, subject, "failed", f"Not sent: {response}")
            else:
                conn.execute("UPDATE send_items SET status='uncertain', note=? WHERE id=?", (response, item["id"]))
                # Test-mode copies went to your own address, so the real draft stays unsent.
                conn.execute("UPDATE drafts SET send_status=? WHERE id=?", ("uncertain" if live else "not_sent", draft["id"]))
                _log(conn, batch, draft, to, subject, "uncertain", response)
            if stop_reason:
                _cancel_batch(conn, batch_id, stop_reason, status="stopped")
                return


def _wait_or_cancelled(batch_id: int, seconds: int) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))
        with db.transaction() as conn:
            if db.row(conn, "SELECT status FROM send_batches WHERE id=?", (batch_id,))["status"] != "sending":
                return True
    return False


def resolve_uncertain(log_id: int, resolution: str) -> None:
    """User checked their Sent folder and tells us what actually happened."""
    if resolution not in ("sent", "not_sent"):
        raise SendError("Resolution must be 'sent' or 'not_sent'.")
    with db.transaction() as conn:
        entry = db.row(conn, "SELECT * FROM send_log WHERE id=?", (log_id,))
        if not entry or entry["result"] != "uncertain":
            raise SendError("This entry is not an uncertain send.")
        conn.execute("UPDATE send_log SET resolved=? WHERE id=?", (resolution, log_id))
        if entry["draft_id"] and entry["mode"] == "live":
            if resolution == "sent":
                conn.execute("UPDATE drafts SET send_status='sent', sent_at=COALESCE(sent_at, ?) WHERE id=?",
                             (entry["created_at"], entry["draft_id"]))
            else:
                # Requires fresh approval before it can be sent again.
                conn.execute("UPDATE drafts SET send_status='failed', approval_status='pending', approved_hash=NULL, "
                             "approved_at=NULL WHERE id=?", (entry["draft_id"],))
                conn.execute("INSERT INTO approval_log(draft_id, action, content_hash, note, created_at) VALUES (?,?,?,?,?)",
                             (entry["draft_id"], "approval_reset_after_uncertain", None, "Marked as not sent", db.now()))
