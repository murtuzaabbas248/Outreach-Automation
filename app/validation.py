"""Email validation and contact data checks."""
from email_validator import EmailNotValidError, validate_email


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def check_email(email: str) -> tuple[bool, str]:
    """Return (is_valid, message). Checks syntax only; no network lookups."""
    email = (email or "").strip()
    if not email:
        return False, "Email address is missing."
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError as exc:
        return False, str(exc)
    return True, ""


def is_valid_email(email: str) -> bool:
    return check_email(email)[0]


def contact_issues(contact: dict) -> list[str]:
    """Problems the user should fix before generating or sending a draft."""
    issues = []
    ok, msg = check_email(contact.get("email", ""))
    if not ok:
        issues.append(f"Invalid email: {msg}")
    if not contact.get("website_name", "").strip():
        issues.append("Missing website name")
    if not contact.get("website_url", "").strip():
        issues.append("Missing website URL")
    if not contact.get("niche", "").strip():
        issues.append("Missing niche")
    if not contact.get("contact_name", "").strip():
        issues.append("No contact name (a generic greeting will be used)")
    if not contact.get("email_verified"):
        issues.append("Email source not marked as verified")
    return issues


# Issues that block draft generation (the others are warnings).
BLOCKING_PREFIXES = ("Invalid email", "Missing website name")


def blocking_issues(contact: dict) -> list[str]:
    return [i for i in contact_issues(contact) if i.startswith(BLOCKING_PREFIXES)]
