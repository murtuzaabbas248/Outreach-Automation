"""Unattended website discovery by Claude Code.

The app starts the Claude Code CLI (`claude -p`) in the background with a fixed prompt.

* Claude may ONLY use web search and Ahrefs' free Domain Rating lookup. Shell commands,
  file edits, web fetching and Gmail are explicitly disallowed, and it runs in an empty
  working folder, not the project.
* Claude replies with JSON: the URLs it found in search results and the DR values Ahrefs
  returned. It never touches the database.
* The app then does the rest itself: imports the URLs (deduplicated by root domain),
  inspects each site with its polite crawler, and stores the DR with its source.
* It uses your Claude subscription's usage allowance. No API key, no paid service.
  Nothing here creates drafts or sends email.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config, csv_sync, db, discovery, free_discovery, seo_metrics
from .domains import root_domain
from .qualify import NICHE_LABELS

ALLOWED_TOOLS = ["WebSearch", "mcp__claude_ai_Ahrefs__public-domain-rating-free"]
DISALLOWED_TOOLS = ["Bash", "PowerShell", "Write", "Edit", "NotebookEdit", "WebFetch", "Agent", "Workflow",
                    "mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Drive", "mcp__claude_ai_Google_Sheets",
                    "mcp__claude_ai_Apollo_io"]
DR_SOURCE = "Domain Rating by Ahrefs (free lookup by Claude auto-discovery, {date})"
SETTING_DAILY = "claude_daily"

_lock = threading.Lock()
_runner = None  # tests replace this with a fake (prompt) -> stdout string


class ClaudeError(Exception):
    pass


# ------------------------------------------------------------------ CLI

def cli_path() -> str:
    if config.CLAUDE_CLI_PATH:
        return config.CLAUDE_CLI_PATH if Path(config.CLAUDE_CLI_PATH).exists() else ""
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if p.exists():
            return str(p)
    return ""


def command(exe: str) -> list[str]:
    return [exe, "-p", "--output-format", "json", "--max-turns", str(config.CLAUDE_DISCOVERY_MAX_TURNS),
            "--allowedTools", ",".join(ALLOWED_TOOLS), "--disallowedTools", ",".join(DISALLOWED_TOOLS)]


def _run_cli(prompt: str) -> str:
    if _runner is not None:
        return _runner(prompt)
    exe = cli_path()
    if not exe:
        raise ClaudeError("Claude Code (the `claude` command) was not found on this computer.")
    workdir = config.DATA_DIR / "claude_agent"  # empty folder: Claude doesn't see the project
    workdir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # use the subscription login, never a paid key
    try:
        r = subprocess.run(command(exe), input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=workdir, env=env, timeout=config.CLAUDE_DISCOVERY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"Claude did not finish within {config.CLAUDE_DISCOVERY_TIMEOUT_SECONDS // 60} minutes.") from None
    if r.returncode != 0 and not r.stdout.strip():
        raise ClaudeError(f"Claude Code exited with code {r.returncode}: {(r.stderr or '').strip()[:300]}")
    return r.stdout


# ------------------------------------------------------------------ prompt and parsing

def known_domains(limit: int = 400) -> list[str]:
    with db.transaction() as conn:
        return [r["domain"] for r in conn.execute("SELECT domain FROM websites ORDER BY discovered_at DESC LIMIT ?", (limit,))]


def previous_queries(limit: int = 60) -> list[str]:
    out = []
    with db.transaction() as conn:
        for r in conn.execute("SELECT queries FROM claude_runs ORDER BY id DESC LIMIT 20"):
            out += json.loads(r["queries"] or "[]")
    return list(dict.fromkeys(out))[:limit]


def build_prompt(niches: list[str], per_niche: int) -> str:
    labels = ", ".join(f"{k} ({NICHE_LABELS[k]})" for k in niches)
    return f"""You are helping a content writer find websites that accept guest posts.
Niches (use these exact keys in your answer): {labels}.
Goal: about {per_niche} NEW publisher websites per niche, each with its own page inviting guest posts or contributors.

Steps:
1. For each niche run 2 to 4 web searches with varied wording, for example: <topic> "write for us",
   <subtopic> "guest post guidelines", <topic> "become a contributor", <topic> blog "submit an article".
   Use different subtopics than before. Do not repeat these earlier searches: {json.dumps(previous_queries())}
2. Keep only a single publisher's own contributor/guidelines page. Exclude: list articles ("100 sites that accept
   guest posts"), SEO agencies and link marketplaces, sites that charge writers, social networks, Medium, LinkedIn,
   Quora, Reddit, Substack, and every domain on this list (already in the database): {json.dumps(known_domains())}
3. Call mcp__claude_ai_Ahrefs__public-domain-rating-free ONCE with all kept domains as "targets". If that tool is
   unavailable or fails, leave "dr" empty.
4. Reply with ONLY this JSON object and nothing else:
{{"sites": [{{"url": "https://...", "niche": "education", "query": "the search that found it", "title": "page title"}}],
 "dr": {{"example.com": 42.0}}, "queries": ["every search you ran"], "notes": "one short sentence"}}

Rules: include only URLs that appeared in your search results, and only DR numbers the Ahrefs tool returned.
Never guess or invent a URL, email or metric. Do not send email or use any tool other than web search and the
Ahrefs Domain Rating lookup."""


def parse_reply(stdout: str) -> tuple[dict, dict]:
    """Return (payload, usage) from `claude -p --output-format json` output."""
    try:
        outer = json.loads(stdout)
    except ValueError:
        raise ClaudeError("Claude Code's output was not JSON.") from None
    usage = {"turns": outer.get("num_turns"), "duration_seconds": round((outer.get("duration_ms") or 0) / 1000),
             "usage_cost_equivalent_usd": outer.get("total_cost_usd"), "is_error": bool(outer.get("is_error"))}
    if outer.get("is_error"):
        raise ClaudeError(f"Claude reported an error: {str(outer.get('result') or outer.get('subtype'))[:300]}")
    text = str(outer.get("result") or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ClaudeError("Claude's reply did not contain the JSON result.")
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        raise ClaudeError("Claude's JSON result could not be read.") from None
    if not isinstance(payload, dict):
        raise ClaudeError("Claude's JSON result had the wrong shape.")
    return payload, usage


def clean_payload(payload: dict, niches: list[str]) -> tuple[dict[tuple[str, str], list[str]], dict[str, float], list[str]]:
    """Group usable URLs by (niche, query); keep only valid DR numbers. Nothing is trusted blindly:
    the app's own crawler still inspects every site."""
    groups: dict[tuple[str, str], list[str]] = {}
    for s in payload.get("sites") or []:
        if not isinstance(s, dict):
            continue
        url, niche = str(s.get("url") or "").strip(), str(s.get("niche") or "").strip()
        if not url.lower().startswith(("http://", "https://")) or niche not in niches:
            continue
        query = re.sub(r"\s+", " ", str(s.get("query") or "")).strip()[:200]
        groups.setdefault((niche, query), []).append(url)
    dr = {}
    for d, v in (payload.get("dr") or {}).items() if isinstance(payload.get("dr"), dict) else []:
        val = seo_metrics.valid_metric(v)
        if val is not None and root_domain(str(d)):
            dr[root_domain(str(d))] = val
    queries = [re.sub(r"\s+", " ", str(q)).strip()[:200] for q in (payload.get("queries") or []) if str(q).strip()][:40]
    return groups, dr, queries


# ------------------------------------------------------------------ run

def _update(run_id: int, **fields) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE claude_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), run_id))


def _wait_for_discovery_idle(max_seconds: int = 900) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        with db.transaction() as conn:
            discovery.recover_interrupted(conn)
            if not db.row(conn, "SELECT id FROM discovery_runs WHERE status='running'"):
                return
        time.sleep(5)


def run(trigger: str, niches: list[str] | None = None, per_niche: int | None = None, run_id: int | None = None) -> dict:
    """Do one complete Claude discovery round (blocking). Returns the run row."""
    niches = [n for n in (niches or list(NICHE_LABELS)) if n in NICHE_LABELS] or list(NICHE_LABELS)
    per_niche = max(1, min(per_niche or config.CLAUDE_DISCOVERY_PER_NICHE, 20))
    if run_id is None:
        run_id = _create(trigger, niches)
    try:
        payload, usage = parse_reply(_run_cli(build_prompt(niches, per_niche)))
        groups, dr, queries = clean_payload(payload, niches)
        queued, known, found = 0, 0, 0
        for (niche, query), urls in groups.items():
            found += len(urls)
            r = free_discovery.import_urls("\n".join(urls), niche, f"Claude auto-discovery: {query}" if query else
                                           "Claude auto-discovery", source=f"claude:{run_id}")
            queued += len(r["queued"])
            known += len(r["already_known"])
        _update(run_id, status="inspecting", sites_found=found, queued=queued, already_known=known,
                queries=json.dumps(queries), usage=json.dumps(usage), message=str(payload.get("notes") or "")[:300])
        disc_id = None
        if queued:
            _wait_for_discovery_idle()
            result = discovery.run_discovery("claude", force=True)
            disc_id = result.get("run_id")
        applied = 0
        if dr:
            rep = seo_metrics.import_values(dr, "dr", DR_SOURCE.format(date=db.local_today()))
            applied = rep["updated"] + rep["unchanged"]
            csv_sync.export()
        msg = (f"Claude found {found} site(s): {queued} new, {known} already in your database. "
               + (f"Inspected in discovery run #{disc_id}. " if disc_id else "")
               + (f"DR stored for {applied} site(s)." if dr else "No DR values were returned."))
        _update(run_id, status="completed", finished_at=db.now(), discovery_run_id=disc_id, dr_values=applied, message=msg)
    except Exception as exc:  # recorded for the dashboard; nothing already imported is lost
        _update(run_id, status="failed", finished_at=db.now(), message=f"{type(exc).__name__}: {exc}"[:500])
    with db.transaction() as conn:
        return db.row(conn, "SELECT * FROM claude_runs WHERE id=?", (run_id,))


def _create(trigger: str, niches: list[str]) -> int:
    with db.transaction() as conn:
        cur = conn.execute("INSERT INTO claude_runs(trigger, status, niches, started_at, run_date) VALUES (?,?,?,?,?)",
                           (trigger, "running", json.dumps(niches), db.now(), db.local_today()))
        return cur.lastrowid


def start_in_background(trigger: str, niches: list[str] | None = None, per_niche: int | None = None) -> dict:
    if _runner is None and not cli_path():
        return {"started": False, "message": "Claude Code (the `claude` command) was not found on this computer."}
    if not _lock.acquire(blocking=False):
        return {"started": False, "message": "Claude is already looking for websites. Please wait for it to finish."}
    with db.transaction() as conn:
        if db.row(conn, "SELECT id FROM claude_runs WHERE status IN ('running','inspecting')"):
            _lock.release()
            return {"started": False, "message": "Claude is already looking for websites. Please wait for it to finish."}
    niches = [n for n in (niches or list(NICHE_LABELS)) if n in NICHE_LABELS] or list(NICHE_LABELS)
    run_id = _create(trigger, niches)

    def _go():
        try:
            run(trigger, niches, per_niche, run_id=run_id)
        finally:
            _lock.release()

    threading.Thread(target=_go, daemon=True, name="claude-discovery").start()
    return {"started": True, "run_id": run_id,
            "message": "Claude is searching for new websites. This usually takes 5 to 15 minutes; new sites appear in the table when it's done."}


# ------------------------------------------------------------------ daily option and status

def daily_enabled() -> bool:
    with db.transaction() as conn:
        return db.get_settings(conn).get(SETTING_DAILY, "false") == "true"


def set_daily(enabled: bool) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (SETTING_DAILY, "true" if enabled else "false"))


def on_startup() -> dict:
    """Runs left unfinished by a previous app process are marked interrupted; then, if the
    daily option is on and Claude hasn't run today, start one round."""
    with db.transaction() as conn:
        conn.execute("UPDATE claude_runs SET status='interrupted', finished_at=?, message='The app was closed during this run.' "
                     "WHERE status IN ('running','inspecting')", (db.now(),))
        ran_today = db.row(conn, "SELECT id FROM claude_runs WHERE run_date=? AND status IN ('completed','running','inspecting')",
                           (db.local_today(),))
    if not daily_enabled():
        return {"started": False, "message": "Claude daily discovery is off."}
    if ran_today:
        return {"started": False, "message": "Claude already looked for websites today."}
    return start_in_background("daily")


def status() -> dict:
    with db.transaction() as conn:
        runs = db.rows(conn, "SELECT * FROM claude_runs ORDER BY id DESC LIMIT 10")
    for r in runs:
        for k in ("niches", "queries"):
            r[k] = json.loads(r[k] or "[]")
        r["usage"] = json.loads(r["usage"] or "{}")
    return {"available": bool(_runner is not None or cli_path()), "daily": daily_enabled(),
            "running": next((r for r in runs if r["status"] in ("running", "inspecting")), None), "runs": runs,
            "per_niche": config.CLAUDE_DISCOVERY_PER_NICHE}
