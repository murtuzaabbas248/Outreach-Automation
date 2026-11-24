"""Rule-based email templates.

Every sentence here is either generic-but-truthful or built from data the user
supplied. Nothing invents relationships, prior contact, compliments, or claims
about having read a specific article.
"""
import hashlib
import random

from .config import sender_email_problem
from .topics import detect_niche, focus_from_url

SUBJECTS = [
    "Guest post idea for {website}: {topic}",
    "Article proposal for {website}",
    "Editorial contribution idea: {topic}",
    "Guest contribution pitch for {website} readers",
]

INTRO_FOLLOW = [
    "I write practical, research-based articles for readers in these fields.",
    "My work focuses on clear, well-sourced articles that readers can put to use.",
    "I focus on articles that explain ideas clearly and back them up with credible sources.",
]

WHY = [
    "I'm reaching out because {website} publishes content on {subject}, and I'd like to propose an article that fits that focus.",
    "I'm contacting you because {website} covers {subject}, and I think a contribution in that area could be useful to your readers.",
    "Since {website} focuses on {subject}, I wanted to ask whether you would consider a guest contribution from me.",
]

AUDIENCE = {
    "education": "educators, students, and parents",
    "edtech": "educators and learning teams adopting new technology",
    "technology": "readers following technology and digital innovation",
    "business_services": "business owners and teams choosing or delivering services",
    "business_growth": "founders and managers working on growth",
    "software": "teams planning software projects and digital change",
    "biometrics": "security, facilities, and IT teams evaluating identification technology",
}

SUBJECT_AREA = {
    "education": "education",
    "edtech": "educational technology",
    "technology": "technology and digital innovation",
    "business_services": "business services",
    "business_growth": "business growth",
    "software": "software and digital transformation",
    "biometrics": "biometric and identification technology",
}

VALUE = [
    "The article would be original and unpublished, written for {audience}, with practical steps and credible sources rather than promotional content.",
    "I would write it as an original piece for {audience}, focused on actionable takeaways and supported by reputable sources.",
    "My aim would be a practical, original article that gives {audience} clear takeaways, with sources cited where relevant and no promotional content.",
]

ASK = [
    "Do you accept guest contributions? If so, could you share your editorial guidelines or the best way to submit a pitch?",
    "Would you be open to a guest contribution? I'd be grateful if you could point me to your contributor guidelines.",
    "Could you let me know whether you accept guest posts, and if so, what your editorial guidelines are?",
]

CLOSING = [
    "Thank you for your time and consideration.",
    "Thank you for considering my proposal.",
    "Thanks for taking the time to read this.",
]

OPT_OUT = "If you'd prefer not to receive emails like this, just reply and let me know, and I won't contact you again."


def _rng(*parts) -> random.Random:
    seed = int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16)
    return random.Random(seed)


def first_name(full_name: str) -> str:
    name = (full_name or "").strip()
    if not name or "@" in name:
        return ""
    parts = name.split()
    if parts[0].lower().rstrip(".") in ("dr", "mr", "ms", "mrs", "prof") and len(parts) > 1:
        # Keep titles together with the surname, e.g. "Dr. Lopez".
        return f"{parts[0]} {parts[-1]}"
    return parts[0]


def greeting(contact: dict) -> str:
    name = first_name(contact.get("contact_name", ""))
    if name:
        return f"Hi {name},"
    if contact.get("website_name"):
        return f"Hello {contact['website_name']} team,"
    return "Hello,"


def signature(settings: dict) -> str:
    lines = ["Best regards,", *([settings["sender_name"]] if settings.get("sender_name") else []),
             "Content Writer & Researcher"]
    for key in ("sender_email", "sender_website", "sender_postal_address"):
        if key == "sender_email" and sender_email_problem(settings.get(key, "")):
            continue  # never print a missing or placeholder address; callers warn instead
        if settings.get(key):
            lines.append(settings[key])
    return "\n".join(lines)


def build_initial(contact: dict, topic: str, alt_topic: str, settings: dict) -> tuple[str, str]:
    """Return (subject, body) for a first outreach email."""
    rng = _rng(contact.get("email", ""), topic)
    niche_key = detect_niche(contact.get("niche", ""))
    website = contact.get("website_name") or "your website"
    focus = focus_from_url(contact.get("article_url", ""))
    subject_area = focus or SUBJECT_AREA[niche_key]

    subject = rng.choice(SUBJECTS).format(website=website, topic=topic)
    if len(subject) > 90:
        subject = f"Article proposal for {website}"

    if alt_topic and alt_topic != topic:
        topic_block = (
            "Here are two topic ideas I think could suit your readers:\n\n"
            f"1. {topic}\n2. {alt_topic}"
        )
    else:
        topic_block = f"I'd like to propose an article titled:\n\n\"{topic}\""

    paragraphs = [
        greeting(contact),
        f"{settings.get('sender_description', '').strip()} {rng.choice(INTRO_FOLLOW)}".strip(),
        rng.choice(WHY).format(website=website, subject=subject_area),
        topic_block,
        rng.choice(VALUE).format(audience=AUDIENCE[niche_key]),
        rng.choice(ASK),
        rng.choice(CLOSING),
        signature(settings),
        OPT_OUT,
    ]
    return subject, "\n\n".join(paragraphs)


def build_followup(contact: dict, parent: dict, settings: dict) -> tuple[str, str]:
    website = parent.get("website_name") or (contact or {}).get("website_name") or "your website"
    subject = f"Following up: {parent['subject']}"
    body = "\n\n".join([
        greeting(contact or {"website_name": website}),
        f"I'm following up on my earlier email about a possible guest article for {website}"
        + (f" on \"{parent['topic']}\"." if parent.get("topic") else "."),
        "I know inboxes get busy, so I'm sending this one short note in case my message was missed. "
        "If you accept contributions, I'd be happy to send an outline or adjust the topic to suit your editorial plans.",
        "If it isn't a good fit, no problem at all; I won't follow up again.",
        signature(settings),
        OPT_OUT,
    ])
    return subject, body
