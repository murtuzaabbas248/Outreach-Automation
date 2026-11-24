"""URL and domain normalization. The normalized root domain is the duplicate key for websites."""
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Common multi-part public suffixes. Without a full public-suffix list we keep
# the last three labels for these, e.g. "blog.example.co.uk" -> "example.co.uk".
MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "org.nz", "ac.nz", "co.za", "org.za", "co.in", "net.in", "org.in", "ac.in", "edu.in", "gov.in",
    "com.pk", "edu.pk", "org.pk", "com.sg", "edu.sg", "com.my", "com.br", "com.mx", "com.tr", "com.ng",
    "co.jp", "co.kr", "com.cn", "com.hk", "com.tw", "com.ph", "com.bd", "com.eg", "com.sa", "co.id", "or.id",
}

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|dclid$|msclkid$|mc_cid$|mc_eid$|ref$|ref_src$|_ga$|_hs|hsa_|yclid$|igshid$|spm$)")


def _host(url_or_domain: str) -> str:
    text = (url_or_domain or "").strip().lower()
    if not text:
        return ""
    if "@" in text and "://" not in text and "/" not in text:
        text = text.rsplit("@", 1)[1]  # an email address
    if "://" not in text:
        text = "http://" + text
    host = urlparse(text).hostname or ""
    return host.strip(".")


def root_domain(url_or_domain: str) -> str:
    """'https://www.Blog.Example.co.uk/path?utm_source=x' -> 'example.co.uk'."""
    host = _host(url_or_domain)
    if not host or re.fullmatch(r"[\d.]+", host):
        return host
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def clean_url(url: str) -> str:
    """Drop tracking parameters, fragments, default ports and a 'www.' prefix's
    case differences, so the same page is not stored twice."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        if re.match(r"^[a-z][a-z0-9+.\-]*:(?!\d)", url, re.I):  # mailto:, tel:, javascript: ...
            return ""
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    try:
        port = p.port
    except ValueError:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k.lower())])
    host = p.hostname.lower()
    netloc = host if not port or port in (80, 443) else f"{host}:{port}"
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), netloc, path, "", query, ""))


def homepage(url_or_domain: str) -> str:
    """Scheme + host of a URL, e.g. 'https://blog.example.com/'."""
    text = (url_or_domain or "").strip()
    if "://" not in text:
        text = "https://" + text
    p = urlparse(text)
    if not p.hostname:
        return ""
    return f"{p.scheme or 'https'}://{p.hostname.lower()}/"


def same_site(url: str, domain: str) -> bool:
    return bool(domain) and root_domain(url) == domain


# Free mailbox providers: their domain says nothing about which website a contact belongs to.
WEBMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "aol.com", "icloud.com",
           "proton.me", "protonmail.com", "zoho.com", "gmx.com", "yandex.com", "mail.com", "msn.com", "me.com"}


def contact_domain(contact: dict) -> str:
    """The website a contact belongs to: its Website URL, else its (non-webmail) email domain."""
    d = root_domain(contact.get("website_url") or "")
    if d:
        return d
    host = root_domain(contact.get("email") or "")
    return "" if host in WEBMAIL else host
