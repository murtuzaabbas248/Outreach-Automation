"""niche_websites_outreach.csv: export from the database (the source of truth)
and merge back edits you made in the file.

* The first 10 columns match the existing contact-import format exactly, so the
  file can also be imported on the Import contacts page.
* The file is written to a temporary file and then swapped in, so a crash never
  leaves a half-written CSV. The previous version is kept as *.backup.csv.
* Before each export, rows you added to the CSV by hand are imported (as
  Needs Review) and blank fields you filled in are copied to the database. The
  database is never overwritten from the CSV, and DA/DR are never read from it.
"""
import csv
import io
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config, db
from .domains import clean_url, homepage, root_domain
from .email_verify import local_status
from .websites import DISCOVERY_STATUSES, insert_website, list_websites, log_change, sync_from_contacts

COLUMNS = [
    "Website Name", "Website URL", "Contact Name", "Email", "Niche", "Relevant Article URL", "Suggested Topic",
    "Personalization Notes", "Outreach Status", "Email Verified",
    "DA", "DR", "Guest Post Guidelines URL", "Contact Source URL", "DA Source", "DR Source", "Last Checked",
    "Discovery Query", "Discovery Status",
]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value) -> str:
    """Text from websites is untrusted: stop spreadsheet apps treating it as a formula."""
    s = "" if value is None else str(value)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "'" + s if s.startswith(FORMULA_PREFIXES) else s


NOT_CHECKED = "Not Checked"


def _metric(v, checked_at=None) -> str:
    """A provider's number, or a label. Never an estimate."""
    if v is None:
        return "No Value Returned" if checked_at else NOT_CHECKED
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def to_row(w: dict) -> list[str]:
    last = max(x for x in (w.get("last_checked") or "", w.get("da_checked_at") or "", w.get("dr_checked_at") or "", ""))
    da_source = w["da_source"] or (NOT_CHECKED if w["da"] is None else "")
    dr_source = w["dr_source"] or (NOT_CHECKED if w["dr"] is None else "")
    return [
        _safe(w["website_name"]), _safe(w["website_url"] or homepage(w["domain"])), _safe(w["contact_name"]), _safe(w["email"]),
        _safe(w["niche"]), _safe(w["article_url"]), _safe(w["suggested_topic"]), _safe(w["personalization_notes"]),
        _safe(w["outreach_label"]), _safe(w["email_verification"]),
        _metric(w["da"], w.get("da_checked_at")), _metric(w["dr"], w.get("dr_checked_at")), _safe(w["guidelines_url"]), _safe(w["contact_source_url"]),
        _safe(da_source), _safe(dr_source), _safe(last[:19].replace("T", " ") if last else ""),
        _safe(w["discovery_query"]), _safe(w["discovery_status"]),
    ]


def render(websites: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for w in websites:
        writer.writerow(to_row(w))
    return buf.getvalue()


def _unsafe(s: str) -> str:
    return s[1:] if s.startswith("'") and s[1:2] in FORMULA_PREFIXES else s


def merge_from_csv(path: Path) -> dict:
    """Bring hand edits from the CSV into the database without overwriting anything."""
    report = {"added": 0, "filled": 0}
    if not path.exists():
        return report
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "Website URL" not in reader.fieldnames:
            return report
        rows = list(reader)
    with db.transaction() as conn:
        for r in rows:
            r = {k: _unsafe((v or "").strip()) for k, v in r.items() if k}
            domain = root_domain(r.get("Website URL", ""))
            if not domain:
                continue
            w = db.row(conn, "SELECT * FROM websites WHERE domain=?", (domain,))
            if w is None:
                status, source = local_status(r.get("Email", ""))
                insert_website(conn, {
                    "domain": domain, "website_name": r.get("Website Name", ""), "website_url": clean_url(r.get("Website URL", "")),
                    "contact_name": r.get("Contact Name", ""), "email": r.get("Email", "").lower(), "niche": r.get("Niche", ""),
                    "email_verification": status, "email_verification_source": source,
                    "contact_source_url": r.get("Contact Source URL", "") or ("Added by you in the CSV" if r.get("Email") else ""),
                    "article_url": clean_url(r.get("Relevant Article URL", "")), "suggested_topic": r.get("Suggested Topic", ""),
                    "personalization_notes": r.get("Personalization Notes", ""),
                    "guidelines_url": clean_url(r.get("Guest Post Guidelines URL", "")),
                    "discovery_status": r.get("Discovery Status") if r.get("Discovery Status") in DISCOVERY_STATUSES else "Needs Review",
                    "discovery_query": "(added in CSV)",
                })
                report["added"] += 1
                continue
            fill = {"contact_name": "Contact Name", "personalization_notes": "Personalization Notes",
                    "suggested_topic": "Suggested Topic", "website_name": "Website Name"}
            for col, header in fill.items():
                if r.get(header) and not w[col]:
                    conn.execute(f"UPDATE websites SET {col}=?, updated_at=? WHERE id=?", (r[header], db.now(), w["id"]))
                    log_change(conn, w["id"], col, "", r[header], "Filled in from your CSV edit")
                    report["filled"] += 1
    return report


def export(path: Path | None = None) -> dict:
    """Merge CSV edits, then write the complete dataset. Returns a report."""
    path = Path(path or config.OUTREACH_CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = merge_from_csv(path)
    with db.transaction() as conn:
        sync_from_contacts(conn)
    rows = list_websites(sort="discovered", direction="asc")
    text = render(rows)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8-sig", newline="")
    warning = ""
    try:
        if path.exists():
            shutil.copy2(path, path.with_name(path.stem + ".backup.csv"))
        os.replace(tmp, path)
        written = path
    except PermissionError:
        # Usually the file is open in Excel. Write next to it instead of failing.
        written = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d-%H%M%S}.csv")
        os.replace(tmp, written)
        warning = f"{path.name} is open in another program, so the export was saved as {written.name}. Close the file and export again."
    return {"path": str(written), "rows": len(rows), "merged": merged, "warning": warning}


def read_header(path: Path | None = None) -> list[str]:
    with open(path or config.OUTREACH_CSV_PATH, encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh))
