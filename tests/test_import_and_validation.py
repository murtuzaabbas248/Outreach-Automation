import io

from openpyxl import Workbook

from app.importer import map_headers
from app.validation import check_email, contact_issues
from tests.conftest import contact_row, import_csv


def test_email_validation():
    assert check_email("editor@example.com")[0]
    assert check_email("first.last+tag@sub.example.co.uk")[0]
    for bad in ["", "plainaddress", "no-at-sign.com", "a@b", "two@@example.com", "spaces in@example.com", "x@.com"]:
        assert not check_email(bad)[0], bad


def test_flexible_headers():
    mapping = map_headers(["Website", "URL", "Editor Name", "Contact Email", "Niche", "Personalization Notes"])
    assert set(mapping.values()) == {"website_name", "website_url", "contact_name", "email", "niche", "notes"}


def test_csv_import_flags_duplicates_and_invalid(client):
    report = import_csv(client, [
        contact_row(1),
        contact_row(2, email="EDITOR1@site1.com"),       # duplicate (case-insensitive)
        contact_row(3, email="not-an-email"),            # invalid, imported but flagged
        "Site 4,site4.com,,,Education,,,,new,yes",       # missing email
    ])
    assert report["imported"] == 2
    assert len(report["duplicates"]) == 1
    assert len(report["invalid"]) == 1
    assert report["missing_email"] == 1

    contacts = {c["email"]: c for c in client.get("/api/contacts").json()}
    assert any("Invalid email" in i for i in contacts["not-an-email"]["issues"])
    assert contacts["editor1@site1.com"]["website_url"] == "https://site1.com"

    # Re-importing the same file adds nothing (duplicates across imports).
    again = import_csv(client, [contact_row(1)])
    assert again["imported"] == 0 and len(again["duplicates"]) == 1


def test_excel_import(client):
    wb = Workbook()
    ws = wb.active
    ws.append(["Website Name", "Website URL", "Contact Name", "Email", "Niche", "Relevant Article URL", "Email Verified"])
    ws.append(["Edu Blog", "https://edu.example", "Sam Lee", "sam@edu.example", "EdTech", "https://edu.example/category/ai-tools", "yes"])
    ws.append(["Biz Blog", "https://biz.example", None, "info@biz.example", "Business services", None, "no"])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/import", files={"file": ("list.xlsx", buf.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    contacts = {c["email"]: c for c in client.get("/api/contacts").json()}
    assert contacts["sam@edu.example"]["email_verified"] == 1
    assert contacts["info@biz.example"]["email_verified"] == 0
    assert any("No contact name" in i for i in contacts["info@biz.example"]["issues"])


def test_rejects_unknown_file_type_and_missing_email_column(client):
    r = client.post("/api/import", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400
    r = client.post("/api/import", files={"file": ("x.csv", b"Website,URL\nA,https://a.com\n", "text/csv")})
    assert r.status_code == 400 and "email column" in r.json()["detail"]


def test_contact_issues_missing_fields():
    issues = contact_issues({"email": "a@b.com", "website_name": "", "website_url": "", "niche": "", "contact_name": "", "email_verified": 0})
    assert "Missing website name" in issues and "Missing niche" in issues
