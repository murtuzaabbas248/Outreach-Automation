"""SQLite database access and schema."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

_path: Path = config.DATABASE_PATH
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    website_name TEXT NOT NULL DEFAULT '',
    website_url TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    email_norm TEXT NOT NULL UNIQUE,
    niche TEXT NOT NULL DEFAULT '',
    article_url TEXT NOT NULL DEFAULT '',
    suggested_topic TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    outreach_status TEXT NOT NULL DEFAULT 'new',
    email_verified INTEGER NOT NULL DEFAULT 0,
    topic_options TEXT NOT NULL DEFAULT '[]',
    selected_topic TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'initial',          -- initial | followup
    parent_draft_id INTEGER REFERENCES drafts(id),
    to_email TEXT NOT NULL,
    website_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    draft_status TEXT NOT NULL DEFAULT 'generated', -- generated | edited | cancelled
    approval_status TEXT NOT NULL DEFAULT 'pending',-- pending | approved | rejected
    approved_hash TEXT,
    approved_at TEXT,
    send_status TEXT NOT NULL DEFAULT 'not_sent',   -- not_sent | queued | sending | sent | failed | uncertain | cancelled
    due_at TEXT,
    sent_at TEXT,
    provider_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    content_hash TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS send_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,           -- awaiting_confirmation | sending | completed | cancelled | stopped
    token TEXT NOT NULL,
    mode TEXT NOT NULL,             -- live | test | dry_run
    provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    finished_at TEXT,
    stop_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS send_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES send_batches(id),
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    approved_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', -- queued | sending | sent | failed | uncertain | cancelled | skipped
    note TEXT NOT NULL DEFAULT '',
    attempted_at TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER,
    draft_id INTEGER,
    recipient TEXT NOT NULL,
    recipient_norm TEXT NOT NULL,
    actual_recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'initial',
    mode TEXT NOT NULL,
    provider TEXT NOT NULL,
    result TEXT NOT NULL,           -- sent | failed | uncertain | simulated
    provider_response TEXT NOT NULL DEFAULT '',
    resolved TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    email_norm TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Website discovery. One row per normalized root domain (the duplicate key).
CREATE TABLE IF NOT EXISTS websites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    website_name TEXT NOT NULL DEFAULT '',
    website_url TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_verification TEXT NOT NULL DEFAULT 'Unknown',  -- Verified | Syntax Valid | Catch-All | Invalid | Unknown
    email_verification_source TEXT NOT NULL DEFAULT '',
    contact_source_url TEXT NOT NULL DEFAULT '',
    niche TEXT NOT NULL DEFAULT '',
    article_url TEXT NOT NULL DEFAULT '',
    article_title TEXT NOT NULL DEFAULT '',
    article_retrieved INTEGER NOT NULL DEFAULT 0,          -- 1 only if the article page itself was fetched
    guidelines_url TEXT NOT NULL DEFAULT '',
    accepts_guest_posts TEXT NOT NULL DEFAULT 'Unknown',  -- Yes | No | Unknown
    suggested_topic TEXT NOT NULL DEFAULT '',
    personalization_notes TEXT NOT NULL DEFAULT '',
    outreach_status TEXT NOT NULL DEFAULT 'New',           -- used only while no contact is linked
    discovery_status TEXT NOT NULL DEFAULT 'New',          -- New | Qualified | Needs Review | Rejected
    status_locked INTEGER NOT NULL DEFAULT 0,              -- 1 after you change the status by hand
    confidence INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '[]',
    review_reason TEXT NOT NULL DEFAULT '',
    discovery_query TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    da REAL,
    da_source TEXT NOT NULL DEFAULT '',
    da_checked_at TEXT,
    dr REAL,
    dr_source TEXT NOT NULL DEFAULT '',
    dr_checked_at TEXT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    crawled_at TEXT,
    last_checked TEXT,
    discovered_at TEXT NOT NULL,
    discovered_date TEXT NOT NULL,                         -- local system date (YYYY-MM-DD)
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS website_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    website_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    niche TEXT NOT NULL DEFAULT '',
    results TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    UNIQUE(provider, query)
);

CREATE TABLE IF NOT EXISTS email_checks (
    email_norm TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    raw_status TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,              -- local system date
    trigger TEXT NOT NULL,               -- startup | manual | scheduled | cli
    status TEXT NOT NULL,                -- running | completed | failed | interrupted
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT NOT NULL DEFAULT '{}',
    errors TEXT NOT NULL DEFAULT '[]',
    api_usage TEXT NOT NULL DEFAULT '{}'
);

-- Free discovery: website URLs you collected yourself (browser searches, saved pages)
-- waiting to be inspected. One pending row per root domain at most.
CREATE TABLE IF NOT EXISTS import_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    niche TEXT NOT NULL DEFAULT '',        -- niche key hint ('' = detect from the site)
    query TEXT NOT NULL DEFAULT '',        -- the search it came from (becomes Discovery Query)
    source TEXT NOT NULL DEFAULT '',       -- paste | file:<name> | cli:<name>
    status TEXT NOT NULL DEFAULT 'pending',-- pending | processed | already_known | duplicate | skipped | error
    note TEXT NOT NULL DEFAULT '',
    website_id INTEGER,
    run_id INTEGER,
    created_at TEXT NOT NULL,
    processed_at TEXT
);

-- Unattended discovery by Claude Code (claude -p). Claude only searches and looks up DR;
-- the app imports, inspects and stores everything itself.
CREATE TABLE IF NOT EXISTS claude_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,                -- button | daily
    status TEXT NOT NULL,                 -- running | inspecting | completed | failed | interrupted
    niches TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    run_date TEXT NOT NULL,               -- local date, for the once-a-day option
    finished_at TEXT,
    sites_found INTEGER NOT NULL DEFAULT 0,
    queued INTEGER NOT NULL DEFAULT 0,
    already_known INTEGER NOT NULL DEFAULT 0,
    dr_values INTEGER NOT NULL DEFAULT 0,
    discovery_run_id INTEGER,
    queries TEXT NOT NULL DEFAULT '[]',
    usage TEXT NOT NULL DEFAULT '{}',
    message TEXT NOT NULL DEFAULT ''
);

-- Reply / bounce / follow-up-due alerts. Gmail is read with headers only (gmail.metadata).
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,              -- Gmail message id, or followup:<draft id>
    kind TEXT NOT NULL,                    -- reply | bounce | followup_due
    contact_id INTEGER,
    draft_id INTEGER,
    email TEXT NOT NULL DEFAULT '',        -- the address you emailed
    from_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    received_at TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT,
    action TEXT NOT NULL DEFAULT ''        -- what the app did, e.g. 'Marked Replied; 1 follow-up cancelled'
);

CREATE TABLE IF NOT EXISTS email_threads (
    draft_id INTEGER PRIMARY KEY,
    thread_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_seen (
    message_id TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_import_queue_status ON import_queue(status, domain);
CREATE INDEX IF NOT EXISTS idx_websites_contact ON websites(contact_id);
CREATE INDEX IF NOT EXISTS idx_runs_date ON discovery_runs(run_date, status);
CREATE INDEX IF NOT EXISTS idx_drafts_contact ON drafts(contact_id);
CREATE INDEX IF NOT EXISTS idx_send_log_recipient ON send_log(recipient_norm);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_today() -> str:
    """Today's date on this computer's clock. Used for 'already ran today' decisions."""
    return datetime.now().astimezone().date().isoformat()


def local_date_of(iso_utc: str | None) -> str:
    if not iso_utc:
        return ""
    try:
        return datetime.fromisoformat(iso_utc).astimezone().date().isoformat()
    except ValueError:
        return ""


def set_path(path) -> None:
    """Point the app at a different database file (used by tests)."""
    global _path
    _path = Path(path)


def get_path() -> Path:
    return _path


def connect() -> sqlite3.Connection:
    _path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction():
    """A connection whose changes are committed on success, rolled back on error.

    A process-wide lock serialises writers so approval checks and status
    changes cannot interleave between the web server and the sending thread.
    """
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)
        # Additive column migrations (existing rows keep their data).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)")}
        if "body" not in cols:
            conn.execute("ALTER TABLE notifications ADD COLUMN body TEXT NOT NULL DEFAULT ''")  # reply text, fetched on demand
        for key, value in config.DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        # A crash mid-send leaves items in 'sending'. We cannot know whether the
        # provider accepted them, so they become 'uncertain' and are never retried.
        conn.execute(
            "UPDATE send_items SET status='uncertain', note='App stopped while this email was being sent. Check your Sent folder.' "
            "WHERE status='sending'"
        )
        conn.execute("UPDATE drafts SET send_status='uncertain' WHERE send_status='sending'")
        conn.execute("UPDATE drafts SET send_status='not_sent' WHERE send_status='queued' AND id NOT IN "
                     "(SELECT draft_id FROM send_items WHERE status='queued')")
        conn.execute(
            "UPDATE send_batches SET status='stopped', finished_at=?, stop_reason='Application restarted during sending.' "
            "WHERE status='sending'",
            (now(),),
        )
        conn.execute("UPDATE send_items SET status='cancelled', note='Batch stopped by restart.' WHERE status='queued' "
                     "AND batch_id IN (SELECT id FROM send_batches WHERE status='stopped')")
        conn.execute("UPDATE drafts SET send_status='not_sent' WHERE send_status='queued'")


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_settings(conn) -> dict:
    data = dict(config.DEFAULTS)
    data.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    # Settings are seeded once, so a placeholder saved before .env was filled in
    # would otherwise stick forever. A real SENDER_EMAIL from .env wins over it.
    env_email = config.DEFAULTS["sender_email"]
    if config.sender_email_problem(data.get("sender_email", "")) and not config.sender_email_problem(env_email):
        data["sender_email"] = env_email
    return data
