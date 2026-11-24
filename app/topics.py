"""Rule-based guest post topic suggestions.

Topics come from curated templates per niche. When the user supplies a
category/article URL, its slug (e.g. /category/online-learning/) is used as a
focus phrase. The generator never claims a topic matches an existing article.
"""
import hashlib
import random
import re
from urllib.parse import urlparse

NICHES = {
    "education": "Education",
    "edtech": "Educational Technology (EdTech)",
    "technology": "Technology and Digital Innovation",
    "business_services": "Business Services",
    "business_growth": "Business Growth and Research",
    "software": "Software and Digital Transformation",
    "biometrics": "Biometric Devices and Identification Technology",
}

# Keywords used to map free-text niche values to one of the six niches.
# Order matters: more specific niches are checked first.
NICHE_KEYWORDS = [
    ("biometrics", ["biometric", "biometrics", "fingerprint", "facial recognition", "face recognition", "iris recognition",
                    "access control", "rfid", "identity verification", "identification technology", "time attendance"]),
    ("edtech", ["edtech", "ed tech", "educational technology", "e-learning", "elearning", "learning technology", "lms"]),
    ("software", ["software", "saas", "digital transformation", "cloud", "devops", "erp", "crm"]),
    ("business_growth", ["growth", "research", "strategy", "startup", "entrepreneur", "market research", "scale"]),
    ("business_services", ["business service", "b2b", "outsourcing", "consulting", "agency", "services", "finance", "hr", "marketing"]),
    ("technology", ["tech", "technology", "innovation", "ai", "digital", "gadget", "cyber", "data"]),
    ("education", ["education", "school", "teacher", "teaching", "student", "learning", "university", "college", "academic", "study"]),
]

TOPIC_BANK = {
    "education": [
        "How Teachers Can Use Formative Assessment to Spot Learning Gaps Early",
        "Practical Ways to Build Study Habits That Last Beyond Exam Season",
        "What Research Says About Spaced Practice and How Students Can Apply It",
        "Supporting First-Generation College Students: A Checklist for Educators",
        "Project-Based Learning on a Tight Timetable: A Realistic Planning Guide",
        "Helping Students Evaluate Online Sources: A Classroom Framework",
        "Reducing Teacher Workload Without Lowering Feedback Quality",
        "Parent–Teacher Communication Strategies That Actually Get Responses",
        "A Practical Guide to {focus} for Busy Educators",
        "Common Misconceptions About {focus} and What the Evidence Shows",
    ],
    "edtech": [
        "Choosing an LMS: Questions Schools Should Ask Before Signing a Contract",
        "Using AI Tutors Responsibly: Guardrails for Schools and Universities",
        "Measuring the Real Impact of EdTech: Metrics Beyond Login Counts",
        "Designing Accessible Online Courses: A WCAG Checklist for Course Creators",
        "Student Data Privacy in EdTech: What Educators Need to Know",
        "Microlearning for Adult Learners: When It Works and When It Doesn't",
        "Blended Learning Models Compared: Rotation, Flex, and Flipped Classrooms",
        "How to Train Teachers on New Classroom Technology Without Burnout",
        "Getting Started With {focus}: A Step-by-Step Guide for Educators",
        "{focus} in Practice: Lessons Schools Learn During Rollout",
    ],
    "technology": [
        "Explaining Generative AI to Non-Technical Teams: A Plain-Language Primer",
        "Edge Computing vs Cloud: Which Workloads Belong Where",
        "Building a Basic Cybersecurity Routine for Small Teams",
        "How to Evaluate a New Technology Before Adopting It: A Decision Framework",
        "The Hidden Costs of Technical Debt and How to Make Them Visible",
        "Responsible AI Adoption: Policies Every Organisation Should Draft First",
        "What No-Code Tools Can and Cannot Replace in Product Development",
        "Data Literacy for Everyone: Skills Teams Need in 2026",
        "{focus} Explained: Key Concepts, Use Cases, and Limitations",
        "A Beginner's Roadmap to {focus} for Growing Organisations",
    ],
    "business_services": [
        "When to Outsource vs Build In-House: A Cost and Risk Comparison",
        "How Small Businesses Can Choose the Right Accounting Service",
        "Writing Service-Level Agreements Clients and Vendors Both Understand",
        "Onboarding New B2B Clients: A Process That Reduces Early Churn",
        "Pricing Professional Services: Hourly, Retainer, or Value-Based?",
        "Building Trust in B2B Services Through Transparent Reporting",
        "How to Audit Your Vendor Stack and Cut Overlapping Services",
        "Remote Service Delivery: Keeping Quality Consistent Across Time Zones",
        "Choosing a {focus} Provider: Criteria Businesses Often Overlook",
        "How {focus} Services Are Changing for Small and Mid-Sized Businesses",
    ],
    "business_growth": [
        "Customer Research on a Small Budget: Interviews, Surveys, and Analytics",
        "Turning Market Research Into Decisions: A Practical Framework",
        "Unit Economics for Founders: The Numbers to Check Before Scaling",
        "How to Identify Your Most Profitable Customer Segments",
        "Building a Growth Plan Around Retention Instead of Acquisition",
        "Competitive Analysis Without Copying: Finding Real Differentiation",
        "Using Cohort Analysis to Understand Why Customers Stay or Leave",
        "Testing a New Market Before Committing Resources",
        "Research-Backed Approaches to {focus} for Growing Companies",
        "Mistakes Businesses Make With {focus} and How to Avoid Them",
    ],
    "software": [
        "Digital Transformation Projects That Stall: Common Causes and Fixes",
        "Legacy System Modernisation: Rehost, Refactor, or Replace?",
        "Choosing SaaS Tools That Integrate: An Evaluation Checklist",
        "Change Management for Software Rollouts: Getting Staff on Board",
        "Measuring ROI on Digital Transformation Beyond Cost Savings",
        "Workflow Automation for Non-Developers: Where to Start",
        "API-First Thinking for Business Leaders: Why It Matters",
        "Data Migration Planning: Avoiding Downtime and Data Loss",
        "A Practical Implementation Plan for {focus}",
        "{focus}: What Decision-Makers Should Know Before Investing",
    ],
    "biometrics": [
        "Fingerprint vs Facial Recognition for Workplace Access: Accuracy, Cost, and Privacy",
        "Biometric Data Privacy: What Organisations Must Do Before Deploying Scanners",
        "Choosing a Biometric Time and Attendance System: A Buyer's Checklist",
        "Liveness Detection Explained: How Biometric Systems Resist Spoofing",
        "RFID Cards vs Biometrics for Access Control: Which Fits Your Site?",
        "Multi-Factor Authentication With Biometrics: Practical Deployment Tips",
        "Reducing False Rejections in Biometric Systems: Causes and Fixes",
        "Biometric Identification in Schools and Campuses: Benefits and Safeguards",
        "A Practical Guide to {focus} for Security and Facilities Teams",
        "{focus}: Common Implementation Mistakes and How to Avoid Them",
    ],
}

GENERIC_SLUG_WORDS = {"category", "categories", "tag", "tags", "blog", "blogs", "news", "articles", "article",
                      "posts", "post", "topics", "topic", "section", "page", "en", "index", "html", "php", "www"}


def detect_niche(niche_text: str) -> str:
    text = (niche_text or "").lower()
    if not text:
        return "education"
    for key, label in NICHES.items():
        if text == key or text == label.lower():
            return key
    if text.strip() in ("a", "b", "c", "d", "e", "f"):
        return list(NICHES)["abcdef".index(text.strip())]
    for key, words in NICHE_KEYWORDS:
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", text):
                return key
    return "education"


def focus_from_url(url: str) -> str:
    """Turn a category URL slug into a short focus phrase, e.g. 'online learning'."""
    if not url:
        return ""
    path = urlparse(url if "://" in url else "https://" + url).path
    parts = [p for p in path.split("/") if p]
    for part in reversed(parts):
        part = re.sub(r"\.(html?|php|aspx?)$", "", part.lower())
        words = [w for w in re.split(r"[-_+]+", part) if w and not w.isdigit()]
        words = [w for w in words if w not in GENERIC_SLUG_WORDS]
        if 1 <= len(words) <= 4:
            return " ".join(words)
    return ""


def _title_case(phrase: str) -> str:
    small = {"and", "or", "of", "the", "in", "for", "to", "a", "an", "on"}
    out = []
    for i, w in enumerate(phrase.split()):
        if w.lower() in {"ai", "lms", "saas", "hr", "seo", "b2b", "erp", "crm", "stem"}:
            out.append(w.upper() if w.lower() != "saas" else "SaaS")
        elif i and w in small:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def suggest_topics(contact: dict, count: int = 3) -> list[str]:
    niche_key = detect_niche(contact.get("niche", ""))
    focus = focus_from_url(contact.get("article_url", ""))
    seed = int(hashlib.sha256((contact.get("email", "") + niche_key).encode()).hexdigest(), 16)
    rng = random.Random(seed)

    templates = TOPIC_BANK[niche_key]
    plain = [t for t in templates if "{focus}" not in t]
    with_focus = [t for t in templates if "{focus}" in t]
    rng.shuffle(plain)
    rng.shuffle(with_focus)

    chosen = []
    if focus:
        chosen.append(with_focus[0].replace("{focus}", _title_case(focus)))
    chosen.extend(plain[: count - len(chosen)])
    return chosen[:count]


def niche_label(niche_text: str) -> str:
    return NICHES[detect_niche(niche_text)]
