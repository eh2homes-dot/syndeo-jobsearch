"""Pulls (person, title) pairs out of arbitrary HTML and prose.

This is deliberately conservative. Leadership pages have no common structure,
so the extractor prefers missing a person over inventing one: every hit needs
a plausible personal name sitting next to a recognised title keyword inside
the same small block of text.

Nothing here is authoritative. Output is a candidate for human verification,
which is the rule the newsletter runs on anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from bs4 import BeautifulSoup

# Ordered roughly by seniority so the highest matching title in a block wins.
TITLE_PATTERNS = [
    r"chief \w+(?: \w+)? officer",
    r"\bC[EFOTMRPIS]O\b",
    r"founder(?: & | and )?(?:co-?founder)?",
    r"co-?founder",
    r"president",
    r"chairman|chairwoman|chairperson",
    r"managing (?:director|partner)",
    r"general (?:manager|counsel)",
    r"(?:executive |senior |group |global |regional )?vice president(?: of| ,)?[\w\s&,]{0,40}",
    r"\b(?:EVP|SVP|VP)\b(?: of)?[\w\s&,]{0,40}",
    r"head of [\w\s&,]{2,40}",
    r"senior director[\w\s&,]{0,40}|director of [\w\s&,]{2,40}|\bdirector\b",
    r"partner",
    r"principal",
]
_TITLE_RE = re.compile("|".join(f"(?:{p})" for p in TITLE_PATTERNS), re.I)

# Two or three capitalised words, optional middle initial, optional particle.
_NAME_RE = re.compile(
    r"\b([A-Z][a-z][A-Za-z'’\-]{1,20}"
    r"(?:\s+(?:[A-Z]\.|van|von|de|del|da|di|la|Mc|Mac))?"
    r"\s+[A-Z][a-z][A-Za-z'’\-]{1,20}"
    r"(?:\s+[A-Z][a-z][A-Za-z'’\-]{1,20})?)\b"
)

# Words that look like names to the regex but aren't people.
_NAME_STOPWORDS = {
    "united states", "new york", "san francisco", "los angeles", "real estate",
    "property management", "privacy policy", "terms of service", "learn more",
    "contact us", "our team", "read more", "view profile", "press release",
    "north america", "south carolina", "north carolina", "new jersey",
    "rhode island", "west virginia", "new hampshire", "las vegas", "st louis",
    "cookie policy", "all rights", "chief executive", "senior living",
    "single family", "multi family", "get started", "case study", "white paper",
}

# Verbs that indicate an actual move in a headline or press release.
MOVE_VERBS = re.compile(
    r"\b(hires?|hired|hiring|appoints?|appointed|appointment|names?|named|"
    r"joins?|joined|joining|promotes?|promoted|promotion|elevates?|taps?|"
    r"welcomes?|adds?|steps? down|departs?|departure|resigns?|resignation|"
    r"retires?|retirement|succeeds?|successor|takes? over|new chief|"
    r"new president|new head of)\b",
    re.I,
)

# Functions Evan's job search already scopes to. Used to rank, not to filter.
PRIORITY_FUNCTIONS = re.compile(
    r"\b(sales|revenue|growth|gtm|go-to-market|marketing|business development|"
    r"partnerships?|customer success|engineering|technology|product|data|"
    r"operations?)\b",
    re.I,
)


@dataclass(frozen=True)
class Person:
    name: str
    title: str

    def key(self) -> str:
        return re.sub(r"[^a-z]", "", self.name.lower())

    def to_dict(self) -> dict:
        return asdict(self)


# Words that are part of a title, never part of a name. The name regex allows
# an optional third word, which otherwise swallows "Chief" from the title
# immediately following ("Sarah Whitlock Chief").
_TITLE_TOKENS = {
    "chief", "vice", "president", "senior", "executive", "managing", "head",
    "director", "partner", "principal", "founder", "cofounder", "co", "global",
    "regional", "national", "group", "general", "deputy", "associate", "assistant",
    "officer", "chairman", "chairwoman", "chair", "lead", "manager", "counsel",
}


def _trim_name(name: str) -> str:
    """Drop leading/trailing tokens that belong to a title, not a name."""
    parts = name.split()
    while parts and parts[-1].lower().strip(".,") in _TITLE_TOKENS:
        parts.pop()
    while parts and parts[0].lower().strip(".,") in _TITLE_TOKENS:
        parts.pop(0)
    return " ".join(parts)


def _looks_like_person(name: str) -> bool:
    low = name.lower().strip()
    # A real name needs at least a first and last.
    if len(name.split()) < 2:
        return False
    if low in _NAME_STOPWORDS:
        return False
    if any(sw in low for sw in _NAME_STOPWORDS):
        return False
    if len(low) < 5 or len(low) > 45:
        return False
    # A title word inside the "name" means the regex grabbed a heading.
    if _TITLE_RE.search(name):
        return False
    return True


def _clean_title(raw: str) -> str:
    t = re.sub(r"\s+", " ", raw).strip(" ,;·|-–—\u00a0")
    return t[:80]


def extract_from_text(text: str, exclude: set[str] | None = None) -> list[Person]:
    """Find (name, title) pairs in a single small block of text.

    `exclude` holds normalized strings that must never be treated as a person —
    in practice the company name, which otherwise wins in headlines like
    "Bilt Rewards hires Dan Moore as SVP of Partnerships".

    When several names sit near one title, the nearest one wins. In English
    the person is almost always adjacent to their title.
    """
    exclude = {e.lower() for e in (exclude or set())}
    found: list[Person] = []

    for tm in _TITLE_RE.finditer(text):
        lo = max(0, tm.start() - 120)
        hi = min(len(text), tm.end() + 120)
        window = text[lo:hi]
        title_start = tm.start() - lo

        best: tuple[int, str] | None = None
        for nm in _NAME_RE.finditer(window):
            candidate = _trim_name(nm.group(1).strip())
            if not _looks_like_person(candidate):
                continue
            if candidate.lower() in exclude:
                continue
            # Distance from the name to the title span.
            if nm.end() <= title_start:
                distance = title_start - nm.end()
            else:
                distance = max(0, nm.start() - title_start)
            if best is None or distance < best[0]:
                best = (distance, candidate)

        if best:
            found.append(Person(best[1], _clean_title(tm.group(0))))

    return found


def extract_from_html(html: str) -> list[Person]:
    """Extract people from a leadership / team / about page."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()

    people: dict[str, Person] = {}

    # Small containers first: these usually wrap one bio card each.
    for el in soup.find_all(["li", "figure", "article", "td", "div", "section"]):
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 600:
            continue
        for p in extract_from_text(text):
            people.setdefault(p.key(), p)

    # Fall back to the whole page if the structured pass found nothing.
    if not people:
        text = soup.get_text(" ", strip=True)
        for chunk in re.split(r"(?<=[.!?])\s+", text):
            for p in extract_from_text(chunk):
                people.setdefault(p.key(), p)

    return sorted(people.values(), key=lambda p: p.name)


def diff_people(
    old: list[Person], new: list[Person]
) -> tuple[list[Person], list[Person], list[tuple[Person, Person]]]:
    """Return (added, removed, retitled) between two snapshots of a page."""
    old_by_key = {p.key(): p for p in old}
    new_by_key = {p.key(): p for p in new}

    added = [p for k, p in new_by_key.items() if k not in old_by_key]
    removed = [p for k, p in old_by_key.items() if k not in new_by_key]
    retitled = [
        (old_by_key[k], new_by_key[k])
        for k in old_by_key.keys() & new_by_key.keys()
        if old_by_key[k].title.lower() != new_by_key[k].title.lower()
    ]
    return added, removed, retitled


def priority_score(title: str) -> int:
    """Rough ranking so the report leads with the moves that matter."""
    t = (title or "").lower()
    score = 0
    if re.search(r"chief|\bC[EFOTMRPIS]O\b|founder|president|chairman", t):
        score += 40
    elif re.search(r"\bEVP\b|executive vice president|managing director", t):
        score += 30
    elif re.search(r"\bSVP\b|senior vice president", t):
        score += 25
    elif re.search(r"\bVP\b|vice president|head of", t):
        score += 20
    elif re.search(r"director", t):
        score += 10
    if PRIORITY_FUNCTIONS.search(t):
        score += 15
    return score
