"""CSV and Excel contact import."""
import csv
import io
import json

from openpyxl import load_workbook

from . import db
from .validation import check_email, normalize_email

# Accepted header spellings for each field (compared lowercase, spaces/underscores ignored).
COLUMN_ALIASES = {
    "website_name": ["websitename", "website", "site", "sitename", "blog", "blogname", "publication"],
    "website_url": ["websiteurl", "url", "siteurl", "domain", "link", "homepage"],
    "contact_name": ["contactname", "contactperson", "editorname", "editor", "name", "contact", "contactpersonoreditorname"],
    "email": ["email", "emailaddress", "contactemail", "contactemailaddress", "e-mail", "mail"],
    "niche": ["niche", "websiteniche", "category", "industry", "topicarea"],
    "article_url": ["articleurl", "relevantarticleurl", "categoryurl", "relevanturl", "relevantarticleorcategoryurl", "articleorcategoryurl"],
    "suggested_topic": ["suggestedtopic", "topic", "guestposttopic", "suggestedguestposttopic"],
    "notes": ["notes", "personalizationnotes", "personalisationnotes", "comments", "note"],
    "outreach_status": ["outreachstatus", "status"],
    "email_verified": ["emailverified", "verified", "sourceverified"],
}

VALID_STATUSES = {"new", "drafted", "contacted", "replied", "opted_out", "do_not_contact", "bounced", "not_interested"}
TRUE_WORDS = {"yes", "y", "true", "1", "verified", "x"}


def _key(header: str) -> str:
    return "".join(ch for ch in str(header or "").lower() if ch not in " _-/()")


def map_headers(headers: list) -> dict[int, str]:
    lookup = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            lookup.setdefault(_key(alias), field)
    mapping = {}
    for idx, header in enumerate(headers):
        field = lookup.get(_key(header))
        if field and field not in mapping.values():
            mapping[idx] = field
    return mapping


def read_rows(filename: str, content: bytes) -> list[dict]:
    """Parse a CSV or Excel file into a list of dicts keyed by our field names."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        raw = [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
    elif name.endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        raw = list(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("Unsupported file type. Please upload a .csv or .xlsx file.")

    raw = [r for r in raw if any(str(c).strip() for c in r)]
    if not raw:
        raise ValueError("The file is empty.")
    mapping = map_headers(raw[0])
    if "email" not in mapping.values():
        raise ValueError("Could not find an email column. Add a header such as 'Email' or 'Contact Email'.")
    result = []
    for r in raw[1:]:
        item = {field: "" for field in COLUMN_ALIASES}
        for idx, field in mapping.items():
            if idx < len(r):
                item[field] = str(r[idx]).strip()
        result.append(item)
    return result


def clean_contact(item: dict) -> dict:
    status = (item.get("outreach_status") or "new").strip().lower().replace(" ", "_").replace("-", "_")
    if status not in VALID_STATUSES:
        status = "new"
    verified = item.get("email_verified")
    if isinstance(verified, str):
        verified = verified.strip().lower() in TRUE_WORDS
    url = (item.get("website_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return {
        "website_name": (item.get("website_name") or "").strip(),
        "website_url": url,
        "contact_name": (item.get("contact_name") or "").strip(),
        "email": (item.get("email") or "").strip(),
        "niche": (item.get("niche") or "").strip(),
        "article_url": (item.get("article_url") or "").strip(),
        "suggested_topic": (item.get("suggested_topic") or "").strip(),
        "notes": (item.get("notes") or "").strip(),
        "outreach_status": status,
        "email_verified": 1 if verified else 0,
    }


def import_contacts(filename: str, content: bytes) -> dict:
    """Import rows. Duplicates (in-file or already stored) are skipped and reported.

    Rows with an invalid email are still imported so they can be fixed in the
    Manage Contacts page; they are flagged and cannot be drafted or sent.
    """
    items = read_rows(filename, content)
    report = {"imported": 0, "duplicates": [], "invalid": [], "missing_email": 0, "suppressed": [], "total_rows": len(items)}
    seen = set()
    with db.transaction() as conn:
        existing = {r["email_norm"] for r in conn.execute("SELECT email_norm FROM contacts")}
        suppressed = {r["email_norm"] for r in conn.execute("SELECT email_norm FROM suppression")}
        for line_no, item in enumerate(items, start=2):
            c = clean_contact(item)
            norm = normalize_email(c["email"])
            if not norm:
                report["missing_email"] += 1
                continue
            if norm in seen or norm in existing:
                report["duplicates"].append({"row": line_no, "email": c["email"]})
                continue
            seen.add(norm)
            ok, msg = check_email(c["email"])
            if not ok:
                report["invalid"].append({"row": line_no, "email": c["email"], "reason": msg})
            if norm in suppressed:
                report["suppressed"].append({"row": line_no, "email": c["email"]})
                c["outreach_status"] = "do_not_contact"
            insert_contact(conn, c)
            report["imported"] += 1
    return report


def insert_contact(conn, c: dict) -> int:
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO contacts (website_name, website_url, contact_name, email, email_norm, niche, article_url, "
        "suggested_topic, notes, outreach_status, email_verified, topic_options, selected_topic, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c["website_name"], c["website_url"], c["contact_name"], c["email"], normalize_email(c["email"]), c["niche"],
         c["article_url"], c["suggested_topic"], c["notes"], c["outreach_status"], c["email_verified"],
         json.dumps([]), c["suggested_topic"], ts, ts),
    )
    return cur.lastrowid
