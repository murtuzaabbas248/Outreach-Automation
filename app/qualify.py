"""HTML analysis and lead qualification for website discovery.

Everything here works only from text actually present on fetched pages. If a
value is not found it stays empty; nothing is guessed or filled in.
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .domains import clean_url, root_domain
from .validation import is_valid_email, normalize_email

# The four niches Zehra writes for. Labels are what's stored and exported.
NICHE_LABELS = {
    "education": "Education",
    "technology": "Technology",
    "biometrics": "Biometric Devices",
    "business_services": "Business Services",
}

NICHE_TERMS = {
    "education": ["education", "educational", "edtech", "e-learning", "elearning", "online learning", "teacher", "teaching",
                  "student", "classroom", "school", "university", "college", "curriculum", "learning", "k-12", "pedagogy",
                  "higher education", "lms", "tutoring", "course"],
    "technology": ["technology", "tech", "software", "saas", "artificial intelligence", "machine learning", "ai", "cloud",
                   "cybersecurity", "information technology", "digital transformation", "developer", "programming", "gadget", "innovation",
                   "data science", "automation", "devops", "iot", "blockchain"],
    "biometrics": ["biometric", "biometrics", "fingerprint", "facial recognition", "face recognition", "iris", "palm vein",
                   "access control", "rfid", "identity verification", "identification", "time and attendance",
                   "attendance system", "smart card", "authentication", "liveness"],
    "business_services": ["business", "b2b", "consulting", "outsourcing", "small business", "entrepreneur", "startup",
                          "marketing", "accounting", "finance", "hr", "human resources", "management", "operations",
                          "business growth", "professional services", "smb", "sales", "productivity", "leadership"],
}
# Terms specific enough that a single hit is meaningful.
STRONG_TERMS = {"biometric", "biometrics", "fingerprint", "facial recognition", "edtech", "e-learning", "elearning", "b2b",
                "rfid", "access control", "saas", "cybersecurity", "higher education", "business growth"}

GUIDELINE_PATTERNS = [
    r"write[\s\-_]*for[\s\-_]*us", r"guest[\s\-_]*post", r"guest[\s\-_]*(article|blog|author|contribut)",
    r"contribut(e|or|ors|ion)[\s\-_]*(to[\s\-_]*us|guidelines|program)?", r"become[\s\-_]*a[\s\-_]*(contributor|writer|author)",
    r"submit[\s\-_]*(an?[\s\-_]*)?(article|post|guest|story|content|pitch)", r"submission[\s\-_]*guidelines",
    r"editorial[\s\-_]*guidelines", r"pitch[\s\-_]*(us|guidelines)", r"accepting[\s\-_]*guest", r"write[\s\-_]*with[\s\-_]*us",
]
GUIDELINE_RE = re.compile("|".join(GUIDELINE_PATTERNS), re.I)
CONTACT_LINK_RE = re.compile(r"contact|about|team|editorial|staff|masthead", re.I)

ACCEPT_POSITIVE = ["we accept guest", "accept guest posts", "accepting guest", "write for us", "submit your article",
                   "submit your guest", "submit a guest", "contributor guidelines", "guest post guidelines",
                   "submission guidelines", "pitch us", "send us your pitch", "send your pitch", "we welcome contributions",
                   "become a contributor", "guest contributors", "editorial guidelines", "how to submit"]
ACCEPT_NEGATIVE = ["no longer accept", "not accepting guest", "not currently accepting", "do not accept guest",
                   "don't accept guest", "dont accept guest", "stopped accepting", "submissions are closed",
                   "closed to submissions", "we are not accepting", "no guest posts", "we do not publish guest"]
SPAM_STRONG = ["casino", "betting", "gambling", "porn", "escort", "viagra", "cialis", "payday loan", "adult content"]
SPAM_WEAK = ["buy guest post", "guest post price", "paid guest post", "sponsored post price", "dofollow link", "do-follow link",
             "all niches", "general niche", "link insertion", "da 50", "da 40", "da 30", "cbd", "crypto casino", "seo packages"]
AGGREGATOR_TITLE = re.compile(r"(\d+\+?\s+)?(sites|websites|blogs)\s+(that\s+)?accept(ing)?\s+guest|guest\s+post(ing)?\s+sites|"
                              r"list\s+of\s+.*(guest|write\s+for\s+us)|write\s+for\s+us\s+(sites|websites|blogs)", re.I)

# Root domains that are platforms, social networks, marketplaces or shared
# hosting. Their pages are not a single publisher's guidelines.
BLOCKED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "pinterest.com", "reddit.com",
    "quora.com", "medium.com", "wikipedia.org", "amazon.com", "github.com", "google.com", "apple.com", "tiktok.com",
    "substack.com", "blogspot.com", "wordpress.com", "tumblr.com", "wix.com", "wixsite.com", "weebly.com", "linktr.ee",
    "fiverr.com", "upwork.com", "freelancer.com", "slideshare.net", "scribd.com", "issuu.com", "yahoo.com", "bing.com",
    "duckduckgo.com", "msn.com", "researchgate.net", "academia.edu", "glassdoor.com", "indeed.com", "craigslist.org",
    "eventbrite.com", "meetup.com", "t.me", "telegram.org", "whatsapp.com", "discord.com", "notion.site", "canva.com",
    "docs.google.com", "forms.gle", "bit.ly", "blogger.com", "hubpages.com", "vocal.media", "ezinearticles.com",
}

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?![\w-])")
BAD_EMAIL_PARTS = ("example.", "domain.com", "email.com", "yourdomain", "yoursite", "sentry", "wixpress", "@2x", ".png", ".jpg",
                   ".jpeg", ".gif", ".webp", ".svg", "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon")
EXCLUDED_LOCAL = {"privacy", "legal", "abuse", "dmca", "gdpr", "careers", "jobs", "hr", "billing", "invoices", "accounts",
                  "unsubscribe", "postmaster", "hostmaster", "security", "complaints", "returns", "orders", "press"}
EMAIL_PRIORITY = [  # (local-part pattern, role, score)
    (r"^(editor|editors|editorial|editorinchief|eic)", "Editor / editorial team", 100),
    (r"guest|contribut|submission|submit|write|writers|pitch", "Guest post / contributor contact", 95),
    (r"^content|^blog|^articles?$|^stories", "Content team", 85),
    (r"^(news|newsroom|tips)$", "Newsroom", 60),
    (r"^(hello|contact|info|team|hi|mail|enquir|inquir|office|admin)", "Public business contact", 40),
    (r"^(support|help|sales|marketing|partnerships?|ads?|advertis)", "General contact", 25),
]
NAME_RE = re.compile(r"(Editor(?:[\s-]in[\s-]Chief)?|Managing Editor|Content (?:Manager|Editor|Lead)|Editorial (?:Manager|Lead|Director)|"
                     r"Guest Post Editor|Contributor Relations)\s*[:\-–—,]\s*([A-Z][a-z]+(?:\s[A-Z][a-z'\-]+){1,2})"
                     r"(?=\s*(?:$|[,.;|(–—]|\s-\s))")
ARTICLE_SKIP = re.compile(r"/(tag|tags|category|categories|author|page|feed|wp-|cart|shop|product|login|register|account|"
                          r"privacy|terms|contact|about|write-for-us|guest-post|search)\b", re.I)


class ParsedPage:
    def __init__(self, url: str, html: str):
        self.url = url
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "template", "svg"]):
            tag.decompose()
        self.title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
        meta = lambda **kw: (soup.find("meta", attrs=kw) or {}).get("content", "") if soup.find("meta", attrs=kw) else ""  # noqa: E731
        self.description = (meta(name="description") or meta(property="og:description"))[:500]
        self.site_name = meta(property="og:site_name")[:200]
        canon = soup.find("link", rel=lambda v: v and "canonical" in v)
        self.canonical = clean_url(urljoin(url, canon["href"])) if canon and canon.get("href") else ""
        self.headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:40])[:3000]
        self.h1 = soup.h1.get_text(" ", strip=True)[:300] if soup.h1 else ""
        raw_text = soup.get_text("\n", strip=True)
        self.lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_text.splitlines() if ln.strip()][:3000]
        self.text = re.sub(r"\s+", " ", raw_text)[:60000]
        self.links: list[tuple[str, str]] = []
        self.mailtos: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                if addr:
                    self.mailtos.append(addr)
                continue
            full = clean_url(urljoin(url, href))
            if full:
                self.links.append((full, a.get_text(" ", strip=True)[:200]))

    @property
    def lower(self) -> str:
        return f"{self.title} {self.description} {self.headings} {self.text}".lower()


def blocked_domain(domain: str) -> bool:
    return domain in BLOCKED_DOMAINS or domain.endswith(".gov") or domain.endswith(".mil")


def is_aggregator(title: str) -> bool:
    return bool(AGGREGATOR_TITLE.search(title or ""))


_TERM_RE = {t: re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])") for terms in NICHE_TERMS.values() for t in terms}


def niche_scores(text: str) -> dict[str, int]:
    text = text.lower()
    scores = {}
    for key, terms in NICHE_TERMS.items():
        score = 0
        for t in terms:
            n = len(_TERM_RE[t].findall(text))
            if n:
                score += min(n, 5) * (3 if t in STRONG_TERMS else 1)
        scores[key] = score
    return scores


def primary_niche(pages: list[ParsedPage], query_niche: str = "") -> tuple[str, int, list[str]]:
    """Return (niche_key or '', score, matched terms). Site-level text (title,
    description, headings) counts double compared with body text."""
    site_text = " ".join(f"{p.title} {p.description} {p.headings} {p.title}" for p in pages)
    body_text = " ".join(p.text[:15000] for p in pages)
    s1, s2 = niche_scores(site_text), niche_scores(body_text)
    total = {k: s1[k] * 2 + s2[k] for k in NICHE_TERMS}
    best = max(total, key=lambda k: (total[k], k == query_niche))
    if total[best] == 0:
        return "", 0, []
    low = f"{site_text} {body_text}".lower()
    matched = [t for t in NICHE_TERMS[best] if _TERM_RE[t].search(low)][:6]
    return best, total[best], matched


def find_guideline_links(page: ParsedPage, domain: str) -> list[str]:
    out = []
    for url, text in page.links:
        if root_domain(url) != domain:
            continue
        if GUIDELINE_RE.search(text) or GUIDELINE_RE.search(urlparse(url).path):
            if url not in out:
                out.append(url)
    return out


def find_contact_links(page: ParsedPage, domain: str) -> list[str]:
    out = []
    for url, text in page.links:
        if root_domain(url) == domain and (CONTACT_LINK_RE.search(urlparse(url).path) or CONTACT_LINK_RE.search(text)):
            if url not in out:
                out.append(url)
    return out[:3]


def looks_like_guidelines(page: ParsedPage) -> bool:
    head = f"{page.title} {page.h1} {urlparse(page.url).path}"
    return bool(GUIDELINE_RE.search(head))


def acceptance(page: ParsedPage | None) -> tuple[str, list[str]]:
    """'Yes' | 'No' | 'Unknown' plus the phrases that decided it."""
    if page is None:
        return "Unknown", []
    low = page.lower
    neg = [p for p in ACCEPT_NEGATIVE if p in low]
    if neg:
        return "No", neg[:3]
    pos = [p for p in ACCEPT_POSITIVE if p in low]
    if pos and looks_like_guidelines(page):
        return "Yes", pos[:3]
    return "Unknown", pos[:3]


def spam_signals(pages: list[ParsedPage]) -> tuple[bool, list[str]]:
    low = " ".join(p.lower for p in pages)
    strong = [w for w in SPAM_STRONG if re.search(r"\b" + re.escape(w) + r"\b", low)]
    weak = [w for w in SPAM_WEAK if w in low]
    return (bool(strong) or len(weak) >= 2), strong + weak


def extract_emails(page: ParsedPage) -> list[str]:
    """Publicly visible addresses only: mailto links and plain text. Obfuscated
    or script-protected addresses are deliberately not decoded."""
    found = []
    for raw in page.mailtos + EMAIL_RE.findall(page.text):
        e = raw.strip().strip(".,;:()[]<>\"'").lower()
        if not is_valid_email(e) or any(b in e for b in BAD_EMAIL_PARTS):
            continue
        if e.split("@")[0] in EXCLUDED_LOCAL:
            continue
        if e not in found:
            found.append(e)
    return found


def email_rank(email: str, domain: str) -> tuple[int, str]:
    local, _, host = email.partition("@")
    score, role = 10, "Other public address"
    for pattern, r, s in EMAIL_PRIORITY:
        if re.search(pattern, local):
            score, role = s, r
            break
    if root_domain(host) == domain:
        score += 20
    return score, role


def choose_email(candidates: list[tuple[str, str, bool]], domain: str) -> dict | None:
    """candidates: (email, source_url, from_guidelines_or_contact_page).
    Off-domain addresses (e.g. Gmail) are only accepted from the guidelines or contact page."""
    best = None
    for email, source, trusted_page in candidates:
        same = root_domain(email.split("@")[1]) == domain
        if not same and not trusted_page:
            continue
        score, role = email_rank(email, domain)
        if trusted_page:
            score += 10
        if best is None or score > best["score"]:
            best = {"email": email, "source_url": source, "role": role, "score": score, "same_domain": same}
    return best


def extract_contact_name(page: ParsedPage) -> tuple[str, str]:
    """(name, role) only when the page states it next to an editorial title, e.g. 'Editor: Jane Doe'."""
    for line in page.lines:  # line by line, so a name never runs into the next sentence
        m = NAME_RE.search(line)
        if m:
            return m.group(2), m.group(1)
    return "", ""


def best_article(pages: list[ParsedPage], domain: str, niche_key: str) -> tuple[str, str]:
    """(url, anchor text) of the internal article link most related to the niche."""
    terms = NICHE_TERMS.get(niche_key, [])
    best, best_score = ("", ""), 0
    seen = set()
    for p in pages:
        for url, text in p.links:
            if url in seen or root_domain(url) != domain:
                continue
            seen.add(url)
            path = urlparse(url).path
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            if ARTICLE_SKIP.search(path) or slug.count("-") < 2 or len(text.split()) < 4 or GUIDELINE_RE.search(text):
                continue
            hay = f"{text} {slug.replace('-', ' ')}".lower()
            score = sum(1 for t in terms if _TERM_RE[t].search(hay))
            if score > best_score:
                best, best_score = (url, text), score
    return best


def site_name(home: ParsedPage | None, fallback_pages: list[ParsedPage], domain: str) -> str:
    for p in ([home] if home else []) + fallback_pages:
        if p.site_name:
            return p.site_name.strip()
    if home and home.title:
        parts = [s.strip() for s in re.split(r"\s+[|\-–—:·]\s+", home.title) if s.strip()]
        if parts:
            # The site name is usually the shortest segment of the homepage title.
            name = min(parts, key=len) if len(parts) > 1 else parts[0]
            if len(name) <= 60:
                return name
    return ""


def assess(*, niche_key: str, niche_score: int, accepts: str, accept_evidence: list[str], spam: bool, spam_words: list[str],
           email: dict | None, guidelines_url: str, blocked_reason: str = "") -> dict:
    """Decide Qualified / Needs Review / Rejected, with a 0-100 confidence and the evidence used."""
    evidence, reasons = [], []
    if blocked_reason:
        return {"status": "Needs Review", "confidence": 0, "evidence": [blocked_reason],
                "reason": f"Could not check automatically: {blocked_reason}"}
    if spam:
        return {"status": "Rejected", "confidence": 90, "evidence": [f"Spam / link-selling signals: {', '.join(spam_words[:5])}"],
                "reason": "Spam or paid-link signals"}
    if not niche_key:
        return {"status": "Rejected", "confidence": 70, "evidence": ["No education, technology, biometrics or business terms found"],
                "reason": "Not relevant to your niches"}
    if accepts == "No":
        return {"status": "Rejected", "confidence": 85, "evidence": [f"Page says: {', '.join(accept_evidence)}"],
                "reason": "Site states it does not accept guest posts"}

    confidence = min(40, niche_score * 2)
    evidence.append(f"Niche {NICHE_LABELS[niche_key]} (score {niche_score})")
    if niche_score < 8:
        reasons.append("weak niche match")
    if accepts == "Yes":
        confidence += 35
        evidence.append(f"Guidelines page: {guidelines_url} ({', '.join(accept_evidence)})")
    elif guidelines_url:
        confidence += 15
        evidence.append(f"Possible guidelines page: {guidelines_url}")
        reasons.append("acceptance not clearly stated")
    else:
        reasons.append("no guest post / contributor page found")
    if email:
        confidence += 25 if email["same_domain"] else 15
        evidence.append(f"Email {email['email']} ({email['role']}) on {email['source_url']}")
    else:
        reasons.append("no public contact email found")

    confidence = min(confidence, 100)
    status = "Qualified" if not reasons else ("Needs Review" if niche_score >= 3 else "Rejected")
    reason = "" if status == "Qualified" else ("Weak relevance: " if status == "Rejected" else "") + "; ".join(reasons)
    return {"status": status, "confidence": confidence, "evidence": evidence, "reason": reason}
