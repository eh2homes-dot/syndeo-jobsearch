"""Reads the people off a leadership / team page.

Team pages have no common markup, but they share a shape: a person's name sits
on its own (a heading, a bold line, a card caption) and their title sits right
next to it. So the page is read as a flat sequence of text lines, in the order a
visitor sees them, and each line that is a plausible personal name is paired
with the nearest title line. Container sizes don't matter, which is what broke
the old reader on cards that carry a long bio.

Precision over recall, because every false "person" becomes a fake move the
week a page shuffles:
  * a name must be 2-4 name-shaped words whose first word is not a common
    English word ("Cookies", "Blog", "Meet", "Board") and none of whose words is
    a common non-name word;
  * a title line must be short and read like a job title, and it can't say
    "at <company>" or "with <company>", which is how a customer quote or a
    press mention reads ("VP at Trinity Financial");
  * JSON-LD Person records, when a page has them, are taken as-is.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

_DATA = Path(__file__).resolve().parent / "data"

_UPPER = "A-ZÀ-ÖØ-Þ"
_LOWER = "a-zß-öø-ÿ"
_TOKEN = re.compile(
    rf"^(?:[{_UPPER}][{_LOWER}]+(?:[-'’][{_UPPER}]?[{_LOWER}]+)*"   # Jane, O'Neil, Smith-Ricco
    rf"|(?:Mc|Mac|O')[{_UPPER}][{_LOWER}]+"                          # McDonald
    rf"|[{_UPPER}][{_LOWER}]*[{_UPPER}][{_LOWER}]+"                  # DeLuca, LaToya
    rf"|[{_UPPER}]\.?(?:[{_UPPER}]\.)?)$"                             # K. / J.R.
)
_PARTICLES = {"van", "von", "de", "del", "della", "da", "di", "la", "le", "du", "st.", "bin", "al"}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_HONORIFICS = re.compile(r"^(?:dr|mr|mrs|ms|mx)\.?\s+", re.I)
# Letters after a name: "Jane Doe, CPM, MBA" / "Jane Doe CPA".
_CREDENTIALS = re.compile(
    r"(?:,?\s+(?:CPM|CCIM|CPA|CFA|MBA|JD|J\.D\.|PhD|Ph\.D\.|MRP|ARM|RMP|MPM|CRMC|SHRM-[A-Z]+|PHR|SPHR|"
    r"PE|P\.E\.|AIA|LEED AP|CAPS|CAM|CCRM|RPA|FMA|CSM|PMP|Esq\.?|CRE|SIOR|MAI|CDPE|ABR|GRI|CRS))+\s*$"
)

# Job-title vocabulary. A title line has to match this.
_TITLE_WORDS = re.compile(
    r"\b(?:chief|officer|ceo|cfo|coo|cto|cmo|cro|cpo|cio|ciso|cso|cao|cco|clo|chro|"
    r"president|chair(?:man|woman|person)?|founder|co-?founder|owner|co-?owner|principal|"
    r"(?:managing|general|senior|founding|operating|venture)\s+partner|partner|"
    r"vice\s+president|v\.?p\.?|svp|evp|avp|head|director|managing\s+director|"
    r"general\s+(?:counsel|manager)|counsel|controller|comptroller|treasurer|secretary|"
    r"broker|manager|lead|leader|executive|superintendent|supervisor|coordinator|"
    r"specialist|administrator|analyst|associate|accountant|engineer|architect|"
    r"advisor|adviser|trustee|board\s+member|member\s+of\s+the\s+board)\b",
    re.I,
)
# Titles that carry a person's other employer are quotes or bios, not this
# company's team ("VP at Trinity Financial", "Director with Freeman Webb").
_TESTIMONIAL = re.compile(r"\b(?:at|with|from|for)\s+[A-Z]", re.U)
_SENTENCE = re.compile(r"[.!?]\s+[A-Z]|[.!?]$")
# "partner" on its own is mostly marketing copy ("Partner with us").
_BARE_PARTNER = re.compile(r"\bpartner(s|ships?)?\b(?!\s*$)", re.I)

# Staff-level titles: kept in the snapshot so the page reads the same each week,
# but a change to one of these isn't a leadership move.
STAFF_TITLE = re.compile(
    r"^(?!.*\b(?:chief|president|vice|vp|svp|evp|head|director|founder|owner|principal|"
    r"partner|general manager|managing|counsel|controller|treasurer|officer|chair)\b).*"
    r"\b(?:manager|coordinator|specialist|administrator|analyst|associate|accountant|"
    r"engineer|architect|supervisor|lead|leader|executive|broker|advisor|adviser)\b",
    re.I,
)

_SEPARATORS = re.compile(r"\s*(?:,|\||–|—|-|:|/|·|•)\s+")
_BLOCK_TAGS = {"p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section",
               "article", "figure", "figcaption", "header", "footer", "dd", "dt", "blockquote",
               "br", "tr", "ul", "ol", "a", "button", "span", "strong", "b", "em", "small"}
_DROP_TAGS = ["script", "style", "noscript", "nav", "footer", "form", "svg", "iframe", "select",
              "template"]

# Name words that are company / page words, never people, beyond the word list.
_EXTRA_NON_NAME = {
    "homes", "group", "capital", "partners", "properties", "realty", "residential",
    "companies", "holdings", "communities", "rentals", "marketing", "digital", "cloud",
    "management", "estate", "property", "services", "solutions", "technologies", "software",
    "advisors", "investments", "ventures", "living", "apartments", "leasing", "inc", "llc",
    "corp", "corporation", "foundation", "fund", "trust", "reit", "bank", "financial",
    "insurance", "mortgage", "housing", "multifamily", "tenants", "landlords", "owners",
    "residents", "agents", "brokers", "investors", "operators", "team", "teams", "leadership",
    "executive", "executives", "board", "directors", "officers", "staff", "meet", "our",
    "the", "and", "of", "for", "with", "view", "read", "bio", "profile", "linkedin", "email",
    "twitter", "learn", "more", "less", "close", "open", "menu", "search", "home", "contact",
    # headline verbs and publication words ("Envoy Appoints", "Coworking Mag")
    "names", "named", "appoints", "appointed", "hires", "hired", "joins", "joined",
    "promotes", "promoted", "taps", "tapped", "welcomes", "adds", "elevates", "announces",
    "selects", "launches", "expands", "unveils", "mag", "magazine", "news", "journal",
    "times", "post", "daily", "weekly", "today", "wire", "review",
    # title words
    "chief", "vice", "president", "senior", "managing", "head", "director", "partner",
    "principal", "founder", "cofounder", "co-founder", "co-chief", "officer", "chairman",
    "chairwoman", "chair", "manager", "counsel", "ceo", "cfo", "coo", "cto", "evp", "svp", "vp",
}


@lru_cache(maxsize=None)
def _words(name: str) -> frozenset:
    path = _DATA / name
    return frozenset(
        w.strip() for w in path.read_text(encoding="utf-8").splitlines()
        if w.strip() and not w.startswith("#")
    )


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def clean_name(text: str) -> str | None:
    """`text` as a person's name, tidied, or None if it isn't one."""
    t = re.sub(r"\s+", " ", text or "").strip("  ,;:|·•-–—\"'“”")
    if not t or len(t) > 50 or any(ch.isdigit() for ch in t) or "@" in t:
        return None
    t = _HONORIFICS.sub("", t)
    t = _CREDENTIALS.sub("", t).strip(" ,")
    if t.isupper() and len(t) > 4:  # "JANE DOE"
        t = " ".join(w.capitalize() if len(w) > 2 or not w.endswith(".") else w for w in t.split())
    parts = t.split()
    core = [p for p in parts if p.lower().strip(",") not in _SUFFIXES]
    if not 2 <= len(core) <= 4:
        return None
    named = 0
    for i, p in enumerate(core):
        low = p.lower()
        if low in _PARTICLES and 0 < i < len(core) - 1:
            continue
        if not _TOKEN.match(p):
            return None
        bare = _fold(p).strip(".'’")
        if len(bare) > 1:
            named += 1
            if bare in _EXTRA_NON_NAME or bare in _words("non_names.txt"):
                return None
    if named < 2:
        return None
    folded = [_fold(p).strip(".") for p in core]
    if len(set(folded)) < len(folded):  # "Dave Dave" is a name repeated by a card's markup
        return None
    first = _fold(core[0]).strip(".")
    if len(first) > 1 and first in _words("non_first_names.txt"):
        return None
    # The last word must be a real word, not an initial ("Jane D.").
    if len(_fold(core[-1]).strip(".")) < 2:
        return None
    return " ".join(parts).rstrip(",")


def clean_title(text: str) -> str | None:
    """`text` as a job title, tidied, or None if it doesn't read like one."""
    t = re.sub(r"\s+", " ", text or "").strip("  ,;:|·•-–—\"'“”()")
    if not t or len(t) > 90 or len(t.split()) > 12:
        return None
    if not _TITLE_WORDS.search(t):
        return None
    if _TESTIMONIAL.search(t) or _SENTENCE.search(t):
        return None
    # "partner" needs to be the title itself, not a word in a slogan.
    if not re.search(r"\b(?:chief|officer|president|chair|founder|owner|principal|vice|v\.?p|"
                     r"svp|evp|head|director|counsel|controller|treasurer|secretary|broker|"
                     r"manager|lead|executive|ceo|cfo|coo|cto|cmo|cro|cio)\b", t, re.I):
        if not re.fullmatch(r"(?:(?:senior|managing|founding|general|operating|venture|"
                            r"equity|associate)\s+)?partner(?:\s*(?:&|and)\s*\w+)?", t, re.I):
            if not re.search(r"\b(?:specialist|coordinator|administrator|analyst|associate|"
                             r"accountant|engineer|architect|advisor|adviser|supervisor|"
                             r"superintendent|trustee|board member)\b", t, re.I):
                return None
    if clean_name(t):  # a name that happens to contain "Lead"
        return None
    return t


def _lines(soup: BeautifulSoup) -> list[str]:
    """Visible text as separate lines, in reading order (see _nodes)."""
    return [text for text, _ in _nodes(soup)]


def _nodes(soup: BeautifulSoup) -> list[tuple[str, NavigableString]]:
    """(line, text node) for the visible text, in reading order. Each text
    node is a line; inline wrappers are not merged, which keeps "Jane Doe" and
    "CEO" apart even when they share a parent. A first and last name split
    into two nodes ("Derek" / "Ramsey") are joined back into one line."""
    out: list[tuple[str, NavigableString]] = []
    for node in soup.descendants:
        if type(node) is NavigableString:  # not comments, doctypes or CDATA
            if node.parent is not None and node.parent.name in _DROP_TAGS:
                continue
            s = re.sub(r"\s+", " ", str(node)).strip()
            if s and s not in ("|", "-", "•", "·", ",", "&nbsp;"):
                out.append((s, node))
    merged: list[tuple[str, NavigableString]] = []
    i = 0
    while i < len(out):
        text, node = out[i]
        if i + 1 < len(out) and " " not in text and " " not in out[i + 1][0]:
            joined = f"{text} {out[i + 1][0]}"
            if clean_name(joined) and not clean_title(text) and not clean_title(out[i + 1][0]):
                merged.append((joined, node))
                i += 2
                continue
        merged.append((text, node))
        i += 1
    return merged


def _ancestors(node) -> list:
    chain, p = [], node.parent
    while p is not None:
        chain.append(p)
        p = p.parent
    return chain


def _closeness(a, b) -> int:
    """Depth of the deepest element holding both nodes: higher = same card."""
    pa = _ancestors(a)
    ids = {id(x) for x in _ancestors(b)}
    for depth_from_a, el in enumerate(pa):
        if id(el) in ids:
            return len(pa) - depth_from_a
    return 0


def _split_inline(line: str) -> tuple[str, str] | None:
    """'Jane Doe, Chief Executive Officer' / 'Jane Doe | CEO' / 'Jane Doe CEO'."""
    parts = _SEPARATORS.split(line, maxsplit=1)
    if len(parts) == 2:
        n, t = clean_name(parts[0]), clean_title(parts[1])
        if n and t:
            return n, t
        n, t = clean_name(parts[1]), clean_title(parts[0])
        if n and t:
            return n, t
    words = line.split()
    for k in (2, 3, 4):
        if len(words) > k:
            n, t = clean_name(" ".join(words[:k])), clean_title(" ".join(words[k:]))
            if n and t:
                return n, t
    return None


def _json_ld(soup: BeautifulSoup) -> list[tuple[str, str]]:
    found = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            typ = o.get("@type")
            if typ == "Person" or (isinstance(typ, list) and "Person" in typ):
                n = clean_name(str(o.get("name", "")))
                t = o.get("jobTitle")
                t = clean_title(t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else ""))
                if n and t:
                    found.append((n, t))
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(s.string or ""))
        except (ValueError, TypeError):
            continue
    return found


def extract_team(html: str) -> list[tuple[str, str]]:
    """[(name, title)] for the people a team page lists, in page order."""
    soup = BeautifulSoup(html, "lxml")
    people = _json_ld(soup)
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    nodes = _nodes(soup)
    lines = [t for t, _ in nodes]
    used: set[int] = set()
    for i, line in enumerate(lines):
        if i in used:
            continue
        name = clean_name(line)
        if name:
            # Candidate titles: the first title line after the name (within 3
            # lines, before the next name) and the line just before it. When
            # both exist, the one sharing the name's card wins; a tie goes to
            # the title after the name, the usual layout.
            after = None
            for j in range(i + 1, min(i + 4, len(lines))):
                if j in used or clean_name(lines[j]):
                    break
                t = clean_title(lines[j])
                if t:
                    after = (j, t)
                    break
            before = None
            if i > 0 and i - 1 not in used:
                t = clean_title(lines[i - 1])
                if t:
                    before = (i - 1, t)
            pick = after or before
            if after and before:
                ca = _closeness(nodes[i][1], nodes[after[0]][1])
                cb = _closeness(nodes[i][1], nodes[before[0]][1])
                pick = before if cb > ca else after
            if pick:
                people.append((name, pick[1]))
                used.update((i, pick[0]))
            continue
        pair = _split_inline(line)
        if pair:
            people.append(pair)
            used.add(i)

    seen, out = set(), []
    for n, t in people:
        k = re.sub(r"[^a-z]", "", _fold(n))
        if k not in seen:
            seen.add(k)
            out.append((n, t))
    return out


# Paths that are never a team page even when they list people: product and
# marketing pages quote customers by name and title.
NOT_TEAM_PATH = re.compile(
    r"/(?:features?|solutions?|products?|platform|pricing|reviews?|testimonials?|customers?|"
    r"case-stud\w*|blog|news|press|insights?|podcasts?|webinars?|events?|resources?/(?!about)|"
    r"lp|integrations?|partners?|careers?|jobs?|demo|tools?|free-tools)(?:/|$)|"
    r"(?:-software|-management|-screening|single-family|multifamily|-teams|for-teams)/?$",
    re.I,
)
TEAM_PATH = re.compile(r"leader|team|management|executive|people|who-we-are|about|company|board", re.I)
SENIOR = re.compile(
    r"\b(?:chief|ceo|cfo|coo|cto|cmo|cro|cpo|cio|president|chair|founder|owner|principal|"
    r"partner|vice president|vp|svp|evp|head of|director|general counsel|controller|treasurer)\b",
    re.I,
)


def page_score(url: str, people: list[tuple[str, str]]) -> tuple[int, int, int]:
    """Rank a candidate page: (is a team-page URL, senior people, all people).
    A product page loses to any team page; among team pages, more leaders win."""
    from urllib.parse import urlparse
    path = urlparse(url).path or "/"
    if NOT_TEAM_PATH.search(path):
        kind = 0
    elif TEAM_PATH.search(path):
        kind = 2
    else:
        kind = 1  # homepage or an unlabelled page
    if kind < 2:
        # Off a team page, "Title, Company" is a customer quote, not staff.
        people = [(n, t) for n, t in people if not _QUOTE_TITLE.search(t)]
    senior = sum(1 for _, t in people if SENIOR.search(t))
    return kind, senior, len(people)


_FUNCTIONS = (r"finance|marketing|sales|operations|product|engineering|technology|people|legal|"
              r"growth|strategy|revenue|data|design|investments?|acquisitions?|development|"
              r"partnerships|culture|hr|it|asset management|capital markets|accounting|research")
_QUOTE_TITLE = re.compile(rf",\s+(?!(?:{_FUNCTIONS})\b)[A-Z][A-Za-z]+", re.U)
