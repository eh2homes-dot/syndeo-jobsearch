"""Reading the jobs on a company's own careers page.

    read_page(url) -> PageResult

Steps, cheapest first. Each step only runs if the one before found nothing:

  1. Download the page. If the address redirects to a job board, read the board.
  2. If the page loads its jobs from a job system (an embedded board, a data
     feed, or links to its postings on that system), read that board directly.
     That gives every posting with a stable id, and it is still the company's
     own list: the board is named by the company's own page, never guessed.
  3. Structured job data on the page (schema.org JobPosting).
  4. Job links written on the page, following its "next page" links.
  5. The same again in a headless browser, for pages that only show their jobs
     after scripts run, clicking "Load more" / "Next" until the list ends.

Never searches for a company by name and never reads a third-party listing
site. If the jobs can't be found from this page, NeedsLink says why.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from . import boards as B
from .boards import Board, JobList, classify
from .browser import Browser, BrowserUnavailable
from .http import FetchError, NotFound, get

log = logging.getLogger("jobsearch.pages")


class NeedsLink(FetchError):
    """The link can't be read as it stands; the message says why."""


class PageDown(FetchError):
    """The page didn't load at all this run, for a reason that may pass (site
    down, timeout, refused). Not proof the link is wrong: a caller that read
    this page fine last time should keep last time's roles."""


@dataclass
class PageResult:
    jobs: list = field(default_factory=list)
    how: str = ""                     # how the jobs were read, in plain words
    board: Optional[Board] = None     # set when they came from a job board the page loads
    final_url: str = ""
    truncated: str = ""
    rendered: bool = False            # True when the headless browser was needed


# ==========================================================================
# JOB LINKS ON A PAGE
# ==========================================================================

# Paths that are unambiguously a single job's page.
_JOB_PATH_STRONG = re.compile(
    r"/(job-listings?|jobs?|job-openings?|positions?|openings?|opportunit(?:y|ies)|"
    r"vacanc(?:y|ies)|postings?)/[^/?#]+/?$", re.I)

# /job/<city>/<title>/<number>/<number>: the shape used by large career sites.
_JOB_PATH_DEEP = re.compile(r"/jobs?/(?:[^/?#]+/){1,4}\d{3,}/?$", re.I)

# /careers/<slug> is a job page on many sites, but also a section page on many
# others, so it only counts when several appear together, like a list.
_JOB_PATH_WEAK = re.compile(r"/careers?/[^/?#]+/?$", re.I)

_NOT_A_JOB = re.compile(
    r"^(benefits|culture|team|teams|life|life-at-.*|values|faqs?|students?|interns?|"
    r"internships?|early-careers|university|why-.*|about.*|perks|locations?|offices?|"
    r"search|apply|login|sign-?in|privacy.*|terms.*|our-.*|meet-.*|diversity.*|"
    r"inclusion.*|blog.*|news.*|events?|people|benefits-.*|how-we-hire|hiring-process|"
    r"open-positions|openings|all-jobs|jobs|positions|search-jobs|job-search|saved-jobs|"
    r"job-alerts?|talent-(community|network)|departments?|categories|category)$", re.I)

_GENERIC_LINK_TEXT = re.compile(
    r"^(learn more|read more|more info|view|view (job|details|role|position|posting|opening)|"
    r"apply|apply now|apply here|details|see (more|details)|open|[›»→>]+)$", re.I)

# Headings that introduce a list of jobs rather than naming one.
_SECTION_HEADING = re.compile(
    r"^(open (positions|roles|jobs)|job (postings|openings|listings)|current (openings|"
    r"opportunities|positions)|careers?|join (us|our team)|we.?re hiring|"
    r"opportunities|available positions|now hiring)$", re.I)

_LOCATION_HINT = re.compile(
    r"(,\s*[A-Z]{2}\b|\bremote\b|\bhybrid\b|\bon-?site\b|\bin office\b|\bUSA\b|\bUnited States\b|"
    r"\b[A-Z]{2}\s+\d{5}\b|\b[A-Z][a-z]+,\s*[A-Z][a-z]+)", re.I)

_BLOCK_TAGS = ["div", "p", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td", "dd", "dt"]
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _join(base: str, href) -> str:
    """urljoin that returns "" for links Python can't parse (e.g. "http://[::1")."""
    if not isinstance(href, str) or not href.strip():
        return ""
    try:
        return urljoin(base, href.strip())
    except ValueError:
        return ""


def site_of(url_or_host: str) -> str:
    """Registrable domain, roughly: jobs.kiterealty.com -> kiterealty.com."""
    try:
        host = urlparse(url_or_host).netloc if "//" in url_or_host else url_or_host
    except ValueError:
        return ""
    host = host.lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _job_href(href: str, link_regex=None) -> str:
    """'strong' / 'weak' if the URL looks like one job's page, else ''.

    With `link_regex` (a job system's own posting-address shape) only links
    matching it count, and they always count as strong.
    """
    try:
        parsed = urlparse(href)
    except ValueError:
        return ""
    path = parsed.path
    if link_regex is not None:
        target = path + ("?" + parsed.query if parsed.query else "")
        return "strong" if (link_regex.search(path) or link_regex.search(target)) else ""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not last or _NOT_A_JOB.match(last):
        return ""
    if _JOB_PATH_STRONG.search(path) or _JOB_PATH_DEEP.search(path):
        return "strong"
    if _JOB_PATH_WEAK.search(path):
        return "weak"
    return ""


# Words that name a person's role. Deliberately not domain words like
# "development", "community" or "sales" on their own: "Growth & Development"
# and "Community Impact" are culture-page cards, not jobs.
_ROLE_NOUN = re.compile(
    r"\b(manager|mgr|mngr|dir|director|engineer|engr|eng|developer|analyst|associate|specialist|coordinator|"
    r"technician|tech|techs|agent|representative|rep|assistant|asst|assoc|lead|leader|officer|president|"
    r"vp|svp|evp|avp|head of|chief|c[eftmrio]o|consultant|designer|accountant|"
    r"administrator|admin|supervisor|intern|internship|executive|superintendent|clerk|"
    r"controller|counsel|attorney|paralegal|scientist|architect|planner|recruiter|"
    r"generalist|partner|principal|advisor|adviser|strategist|producer|writer|editor|"
    r"operator|installer|worker|driver|porter|cook|nurse|caregiver|housekeeper|concierge|"
    r"receptionist|teller|underwriter|closer|processor|appraiser|inspector|estimator|"
    r"buyer|marketer|owner|foreman|mechanic|electrician|plumber|painter|janitor|"
    r"custodian|programmer|tester|auditor|bookkeeper|cashier|server|bartender|guard|"
    r"apprentice|trainee|fellow|ambassador|expert|professional|dispatcher|scheduler|"
    r"merchandiser|therapist|aide|attendant|laborer|landscaper|groundskeeper|carpenter|"
    r"welder|lineman|line worker|splicer|team member|crew member|staff accountant|"
    r"trader|banker|broker|realtor|underwriting|actuary|economist|researcher|maintenance|"
    r"staff|photographer|inspector|handyman|cleaner|lifeguard|housekeeping)s?\b", re.I)


def _plausible_jobs(titles: list, where: str) -> None:
    """Refuse to publish a page-layout read that doesn't look like job listings.

    Raises FetchError naming what was found, so the report can show it. Reads
    that come straight from a job system skip this; only pattern-based reads
    are checked.
    """
    if not titles:
        return
    with_role = sum(1 for t in titles if _ROLE_NOUN.search(t))
    if with_role * 3 < len(titles):
        examples = ", ".join(f"'{t}'" for t in list(dict.fromkeys(titles))[:3])
        raise FetchError(f"found {len(titles)} listings {where}, but they don't look like "
                         f"job titles (e.g. {examples})")


def _link_lines(link) -> list:
    """The separate lines of text inside a link (a job card that is itself a link)."""
    return [s for s in (" ".join(x.split()) for x in link.stripped_strings) if s]


def _job_card(link, page_url: str, link_regex=None) -> tuple:
    """Title and location for one job link, read from the card around it.

    A link that is itself the whole card (heading, location and blurb inside
    one <a>) is read from the inside. Otherwise this walks up from the link
    until it finds a heading, but stops before the container grows to hold a
    second job, so titles never bleed across cards.
    """
    link_text = link.get_text(" ", strip=True)
    target = _join(page_url, link["href"]).split("#")[0].rstrip("/")

    # The link is the card.
    inner = link.find(_HEADINGS)
    lines = _link_lines(link)
    stacked = len(lines) >= 2 and (link.find(_BLOCK_TAGS) is not None or (
        not _LOCATION_HINT.search(lines[0]) and any(_LOCATION_HINT.search(t) for t in lines[1:])))
    if inner is not None or stacked:
        title = inner.get_text(" ", strip=True) if inner is not None else lines[0]
        if title and len(title) <= 140 and not _GENERIC_LINK_TEXT.match(title):
            location = next((t for t in lines if t != title and len(t) <= 60
                             and _LOCATION_HINT.search(t)), "")
            return title, location

    # On a job system's own board every job link is known to be a job, and its
    # text is the title. Looking further afield there would pick up the page's
    # own heading ("Acme Careers") when only one job is listed.
    if link_regex is not None and link_text and len(link_text) <= 140 and not _GENERIC_LINK_TEXT.match(link_text):
        return link_text, ""

    node, card = link, None
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        others = {_join(page_url, a["href"]).split("#")[0].rstrip("/")
                  for a in node.find_all("a", href=True)
                  if _job_href(_join(page_url, a["href"]), link_regex)}
        if len(others - {target}) > 0:
            break
        card = node
        if node.find(_HEADINGS):
            break

    title, heading = "", None
    if card is not None:
        heading = card.find(_HEADINGS)
        if heading:
            title = heading.get_text(" ", strip=True)
    if not title and link_text and not _GENERIC_LINK_TEXT.match(link_text):
        title = link_text

    # Flat layouts: heading, details, "Apply" link, next heading... with no box
    # around each job. Use the nearest heading before the link, but only if no
    # other job link sits between them, so one job's title can't be borrowed by
    # the next. Section headings ("Open Positions") never count as a title.
    if not title:
        h = link.find_previous(["h2", "h3", "h4", "h5", "h6"])
        if h is not None and not _SECTION_HEADING.match(h.get_text(" ", strip=True)):
            clean_run = True
            for el in h.find_all_next("a", href=True):
                if el is link:
                    break
                if _job_href(_join(page_url, el["href"]), link_regex):
                    clean_run = False
                    break
            if clean_run:
                heading, title = h, h.get_text(" ", strip=True)
                card = None
                location = ""
                for el in h.next_elements:
                    if el is link:
                        break
                    if isinstance(el, str):
                        text = el.strip()
                        if text and text != title and len(text) <= 60 and _LOCATION_HINT.search(text):
                            location = text
                            break
                if title and len(title) <= 140:
                    return title, location
    if not title or len(title) > 140 or _GENERIC_LINK_TEXT.match(title):
        return "", ""

    location = ""
    if card is not None:
        for text in card.stripped_strings:
            if text in (title, link_text) or len(text) > 60:
                continue
            if _LOCATION_HINT.search(text):
                location = text
                break
    return title, location


ONPAGE_MAX_PAGES = 15

# A next-page link must look like another page of the same list: ?page=2,
# /page/2/, ?start=20... This is what keeps "Next post" and other "next" links
# elsewhere on a site from being followed.
_PAGE_URL = re.compile(r"([?&](page|pg|p|paged|pagenum|pageno|start|offset|skip|pr)=\d+)|/page/\d+/?($|[?#])", re.I)
_NEXT_TEXT = re.compile(r"^(next|next page|next ›|next »|next >|more|›|»|>|→|>>)$", re.I)


def _next_page(soup, current: str) -> str:
    """The URL of the next page of a paginated job list, or ""."""
    candidates = []
    for tag in soup.find_all(["link", "a"], href=True):
        rel = [x.lower() for x in (tag.get("rel") or [])]
        if "next" in rel:
            candidates.append(tag["href"])
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        label = (a.get("aria-label") or "") + " " + " ".join(a.get("class") or [])
        if _NEXT_TEXT.match(text) or re.search(r"\bnext\b", label, re.I):
            candidates.append(a["href"])
    for href in candidates:
        nxt = _join(current, href)
        if (nxt and site_of(nxt) == site_of(current) and _PAGE_URL.search(nxt)
                and nxt.split("#")[0] != current.split("#")[0]):
            return nxt.split("#")[0]
    return ""


def _read_listing_html(soup, page_url: str, found: dict, link_regex=None) -> int:
    """Add one page's job links to `found` ({href: (kind, title, location)});
    return how many strong job links had no readable title."""
    untitled = 0
    for a in soup.find_all("a", href=True):
        href = _join(page_url, a["href"]).split("#")[0]
        if not href.startswith("http") or site_of(href) != site_of(page_url):
            continue
        if href.rstrip("/") == page_url.rstrip("/").split("#")[0]:
            continue
        if link_regex is None and _PAGE_URL.search(href) and not _job_href(href.split("?")[0]):
            continue           # a pagination link, not a job
        kind = _job_href(href, link_regex)
        if not kind or href in found:
            continue
        title, location = _job_card(a, page_url, link_regex)
        if title:
            found[href] = (kind, title, location)
        elif kind == "strong":
            untitled += 1
    return untitled


def _strip_chrome(soup) -> None:
    for tag in soup.find_all(["nav", "header", "footer"]):
        tag.decompose()


_ID_PARAM = re.compile(r"^(id|jobid|job_id|job|jid|gh_jid|reqid|req_id|req|rid|posting|postingid|"
                       r"opportunityid|oid|positionid|position|vacancy|vacancyid|jobcode|jobreq)$", re.I)


def _posting_id(href: str) -> str:
    """A posting's id: its path. Where a site tells its jobs apart only by a
    query value (/jobs/view?id=12), that value is part of the id. The rule
    looks at one link at a time, so a posting's id never depends on what else
    is listed that week."""
    parsed = urlparse(href)
    path = parsed.path.rstrip("/")
    keep = sorted(f"{k}={v[0]}" for k, v in parse_qs(parsed.query).items() if _ID_PARAM.match(k) and v)
    return f"{path}?{'&'.join(keep)}" if keep else path


def _found_to_jobs(found: dict, trusted: bool, where: str) -> list:
    strong = [(h, v) for h, v in found.items() if v[0] == "strong"]
    weak = [(h, v) for h, v in found.items() if v[0] == "weak"]
    rows = strong + (weak if len(weak) >= 2 else [])
    if not rows:
        return []
    if not trusted:
        _plausible_jobs([v[1] for _, v in rows], where)
    jobs = []
    for href, (_kind, title, location) in rows:
        jid = _posting_id(href)
        jobs.append({"system": "page", "slug": "", "id": jid, "title": title, "url": href,
                     "location": location, "department": "", "posted": "", "posted_rel": "", "ref": ""})
    return jobs


# ==========================================================================
# STRUCTURED JOB DATA (schema.org JobPosting)
# ==========================================================================

def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            yield node
        for key in ("@graph", "itemListElement", "item"):
            if key in node:
                yield from _walk_jsonld(node[key])


def jsonld_jobs(soup, page_url: str) -> list:
    """schema.org JobPosting markup on a page (Paradox, many WordPress job
    plugins, hand-built careers pages)."""
    out = []
    for tag in soup.find_all(attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for j in _walk_jsonld(data):
            locs = j.get("jobLocation") or {}
            locs = locs[0] if isinstance(locs, list) and locs else locs
            addr = (locs.get("address") or {}) if isinstance(locs, dict) else {}
            if not isinstance(addr, dict):
                addr = {"addressLocality": B.as_text(addr)}
            ident = j.get("identifier")
            ident = ident.get("value") if isinstance(ident, dict) else ident
            link = j.get("url") or page_url
            title = B.as_text(j.get("title"))
            if not title:
                continue
            out.append({"system": "jsonld", "slug": "", "id": f"{ident or link}-{title}"[:197],
                        "title": title, "url": B.as_text(link),
                        "location": B.place(addr.get("addressLocality"), addr.get("addressRegion")),
                        "department": "", "posted": B.as_text(j.get("datePosted"))[:10],
                        "posted_rel": "", "ref": ""})
    return out


# ==========================================================================
# WHICH JOB BOARD DOES THIS PAGE LOAD ITS JOBS FROM?
# ==========================================================================

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\)]+", re.I)

# Strength of the evidence that a board belongs to this page.
_LOADED, _EMBEDDED, _MENTIONED, _LINKED = 4, 3, 2, 1


def _candidates(evidence: list, page_url: str) -> list:
    """Rank the job boards a page points at. `evidence` is [(url, strength)].

    Strongest evidence first (the page loaded its jobs from the board, then an
    embedded frame or script, then a mention in the page's code, then ordinary
    links), and within a strength, the board with the most links.
    """
    page_board = classify(page_url)
    score: dict = {}
    for url, strength in evidence:
        url = html_mod.unescape(url or "").replace("\\/", "/").replace("\\u002F", "/")
        if not B.JOB_SYSTEM_HOSTS.search(url[:600]):
            continue
        board = classify(url)
        if not (board.readable or board.rendered):
            continue
        if board.key == page_board.key:
            continue                     # the page is that board
        if board.slug.lower() == board.system:
            continue                     # the vendor's own board (jobs.lever.co/lever), reached via its branding
        best, count, _ = score.get(board.key, (0, 0, board))
        score[board.key] = (max(best, strength), count + 1, board)
    ranked = sorted(score.values(), key=lambda t: (-t[0], -t[1]))
    return [(board, strength, count) for strength, count, board in ranked]


def _static_evidence(html: str, soup, page_url: str) -> list:
    evidence = []
    for tag in soup.find_all("iframe", src=True):
        evidence.append((_join(page_url, tag["src"]), _EMBEDDED))
    for tag in soup.find_all("script", src=True):
        evidence.append((_join(page_url, tag["src"]), _EMBEDDED))
    for a in soup.find_all("a", href=True):
        evidence.append((_join(page_url, a["href"]), _LINKED))
    linked = {u for u, _ in evidence}
    text = html_mod.unescape(html).replace("\\/", "/").replace("\\u002F", "/")
    for m in _URL_IN_TEXT.finditer(text):
        if m.group(0) not in linked:
            evidence.append((m.group(0), _MENTIONED))
    return evidence


_BREEZY_MARK = re.compile(r"breezy\.hr|breezy-portal|bzy-", re.I)


def _breezy_own_domain(html: str, soup, page_url: str) -> Optional[Board]:
    """A Breezy board served from the company's own address (jobs.doorstead.com)."""
    if not _BREEZY_MARK.search(html):
        return None
    if not any(re.match(r"^/p/[0-9a-f]{8,}", urlparse(_join(page_url, a["href"])).path or "")
               for a in soup.find_all("a", href=True)):
        return None
    parsed = urlparse(page_url)
    if parsed.netloc.endswith(".breezy.hr"):
        return None
    return Board(system="breezy", slug=parsed.netloc.lower(),
                 params={"origin": f"{parsed.scheme}://{parsed.netloc}"})


def _try_boards(cands: list, tried: dict, strong: bool, reader_extra: dict) -> tuple:
    """Read the first candidate board that has jobs.

    Returns (hit, empty): `hit` is (board, jobs, strength) for a board with
    jobs; `empty` is the same for a board that answered with none. `tried`
    collects what happened to each candidate: ("jobs"|"empty"|"gone"|"error"|
    "browser", detail).
    """
    hit = empty = None
    for board, strength, _count in cands[:6]:
        if (strength >= _EMBEDDED) != strong or board.key in tried:
            continue
        if not board.readable:
            tried[board.key] = ("browser", board, strength)
            continue             # boards read in the browser are handled by the caller
        try:
            extra = reader_extra if board.system == "workday" else {}
            jobs = B.read_board(board, **extra)
        except NotFound as exc:
            tried[board.key] = ("gone", str(exc))
            continue
        except FetchError as exc:
            tried[board.key] = ("error", str(exc), strength)
            log.debug("    candidate %s failed: %s", board.label(), exc)
            continue
        except Exception as exc:  # noqa: BLE001 - an odd payload from a candidate is not fatal
            tried[board.key] = ("error", f"{type(exc).__name__}: {exc}"[:160], strength)
            continue
        if jobs:
            tried[board.key] = ("jobs", str(len(jobs)))
            return (board, jobs, strength), empty
        tried[board.key] = ("empty", "")
        if empty is None:
            empty = (board, jobs, strength)
    return hit, empty


def _how_board(board: Board, strength: int) -> str:
    verb = {_LOADED: "loads its jobs from", _EMBEDDED: "embeds", _MENTIONED: "uses",
            _LINKED: "links to"}.get(strength, "points to")
    return f"read from the job board this page {verb} ({board.label()}: {board.listing_url})"


# ==========================================================================
# read_page
# ==========================================================================

# Listing sites that republish other companies' jobs. Never a source: a company
# is read from its own careers page or its own job board, or not at all.
THIRD_PARTY = ("builtin.com", "linkedin.com", "glassdoor.com", "indeed.com", "ziprecruiter.com",
               "wellfound.com", "angel.co", "simplyhired.com", "monster.com", "careerbuilder.com",
               "themuse.com", "dice.com", "lensa.com", "jooble.org", "talent.com", "hirebase.org")

_CAREERS_HOST = re.compile(r"(careers?|jobs?|join|work|talent|hiring)\.", re.I)
_LOOKS_LIKE_HTML = re.compile(
    r"<\s*(html|head|body|div|a|p|h[1-6]|section|main|ul|span|meta|title|link|"
    r"style|form|table|img|nav|header|footer|article|!doctype)\b", re.I)

# A page telling its visitors there is nothing open right now.
# Deliberately narrow: careers copy is full of sentences like "no limit to the
# opportunities here" and "if no positions match your skills...", and reading
# one of those as "not hiring" would quietly close a company's roles.
_NOT_THIS = r"(?!\s+(match|fit|suit|that|which|for you|listed below)\b)"
_SAYS_NONE = re.compile(
    r"\b(there are|there.re|we have|we currently have|we.ve got) (currently |presently )?no "
    r"(open |current |available |active )?(job )?(openings|positions|roles|vacancies|jobs)\b" + _NOT_THIS +
    r"|\bno (current|open|available|active)(ly)? (job )?(openings|positions|roles|vacancies|jobs)\b" + _NOT_THIS +
    r"|\bno (job )?(openings|positions|roles|vacancies|jobs) (are )?(currently |presently )?"
    r"(available|open|posted)?\s*(at this time|right now|at the moment|currently)\b"
    r"|\b(not|aren.t|are not) (currently|actively) hiring\b(?!\s+for\b)"
    r"|\b(don.t|do not) (currently )?have any (open |current |available )?(job )?(openings|positions|roles|vacancies|jobs)\b"
    + _NOT_THIS, re.I)

# A page that answered with a bot check instead of its content.
_BLOCKED = re.compile(
    r"just a moment|verif(y|ying) (that )?you are (a )?human|checking your browser|access denied|"
    r"attention required|unusual traffic|are you a robot|request (was )?blocked|pardon our interruption|"
    r"enable javascript and cookies to continue", re.I)


def _visible_text(soup) -> str:
    """Roughly what a visitor reads on a plainly-downloaded page."""
    soup = BeautifulSoup(str(soup), "lxml")
    for tag in soup.find_all(["script", "style", "template", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()
    return soup.get_text(" ", strip=True)


# A link from a careers page to the page holding its full list of jobs.
_FULL_LIST = re.compile(
    r"^(view|see|search|browse|explore|show|find)\s+((all|our|open|current|available|more)\s+)*"
    r"(jobs|positions|openings|roles|opportunities|job openings|open positions|open roles)$"
    r"|^(all|open|current|available)\s+(jobs|positions|openings|roles|opportunities)$"
    r"|^(job search|search jobs|job openings|career opportunities|current opportunities)$", re.I)


def _full_list_link(soup, page_url: str) -> str:
    """Where the page says its full list of jobs is ("View all jobs"), or ""."""
    here = page_url.split("#")[0].rstrip("/")
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        if not _FULL_LIST.match(text):
            continue
        target = _join(page_url, a["href"]).split("#")[0]
        if (target.startswith("http") and target.rstrip("/") != here
                and site_of(target) not in THIRD_PARTY):
            return target
    return ""


def read_page(url: str, **kw) -> PageResult:
    """Jobs listed on (or loaded by) one page. See the module docstring and _read_page.

    One extra step on top of _read_page: many careers pages show a few featured
    jobs, or none, and link to the full list ("View all jobs", "Search jobs").
    When the page has such a link, the list it leads to is read too, and used
    if it has more.
    """
    seen: dict = {}
    first, problem = None, None
    try:
        first = _read_page(url, _seen=seen, **kw)
    except NeedsLink as exc:
        problem = exc
    more = seen.get("full_list", "")
    if more and not kw.get("link_regex") and (first is None or first.board is None):
        try:
            second = _read_page(more, _seen={}, **kw)
        except FetchError:
            second = None
        if second is not None and len(second.jobs) > (len(first.jobs) if first else 0):
            second.how += f"; found by following the page's link to its full list ({more})"
            return second
        if second is not None and first is None:
            return second                    # the full list loaded and has nothing open
    if first is None:
        raise problem
    return first


def _read_page(url: str, *, trusted: bool = False, link_regex: str = "",
               browser: Optional[Browser] = None, follow_boards: bool = True,
               search_terms=None, workday_max_pages: int = 0,
               label: str = "careers page", _seen: Optional[dict] = None) -> PageResult:
    """Jobs listed on (or loaded by) exactly this page.

    `link_regex`     a job system's posting-address shape; when set, the page
                     is that system's own board, so only matching links count
                     and the "do these look like job titles" check is skipped.
    `follow_boards`  read the job board the page loads, when it loads one.
    `browser`        the run's headless browser, or None to never render.

    Three outcomes, kept apart on purpose:
      a PageResult (possibly with no jobs: "nothing open right now")
      NeedsLink    the link is the problem; someone should fix it
      FetchError   couldn't check this run (a site or the browser didn't
                   answer); the caller keeps last run's roles
    """
    if site_of(url) in THIRD_PARTY:
        raise NeedsLink(f"the {label} is a third-party listing site ({site_of(url)}), not the "
                        "company's own careers page or job board")
    rx = re.compile(link_regex, re.I) if link_regex else None
    system_board = rx is not None
    trusted = trusted or system_board
    follow = follow_boards and not system_board
    reader_extra = {k: v for k, v in (("search_terms", search_terms), ("max_pages", workday_max_pages)) if v}
    tried: dict = {}
    notes: list = []          # why each step came up empty
    result = PageResult()

    # ---- 1. plain download
    html, final, soup = "", url, None
    static_error = ""
    try:
        r = get(url, timeout=25)
        html, final = (r.text or "").strip(), r.url
    except FetchError as exc:
        static_error = str(exc)

    landed_on = classify(final) if (html and final != url) else Board()
    redirected_to_board = landed_on.readable or landed_on.rendered
    if html and not system_board and not redirected_to_board:
        if not _LOOKS_LIKE_HTML.search(html[:300000]) and "application/ld+json" not in html[:300000]:
            raise NeedsLink(f"the {label} isn't a web page (it returned a file or raw data)")
        asked, landed = urlparse(url), urlparse(final)
        if asked.path in ("", "/") and not _CAREERS_HOST.match(asked.netloc):
            raise NeedsLink(f"the {label} is a homepage, not a careers page or job board")
        if asked.path not in ("", "/") and landed.path in ("", "/") and not _CAREERS_HOST.match(landed.netloc):
            raise NeedsLink(f"the {label} redirects to the homepage, so it probably doesn't exist")
    moved = ""
    if html and site_of(final) != site_of(url):
        moved = f"the link now redirects to {urlparse(final).netloc} - worth updating; "
    result.final_url = final

    def finish(jobs, how, board=None, truncated="", rendered=False) -> PageResult:
        result.jobs, result.how, result.board = list(jobs), moved + how, board
        result.truncated, result.rendered = truncated or getattr(jobs, "truncated", ""), rendered
        return result

    cands: list = []
    empty_board = None        # a board the page really loads, with nothing open
    says_none = ""            # the page's own words, when it says nothing is open
    saw_page = False          # True once we have looked at the page as a visitor would

    if html:
        soup = BeautifulSoup(html, "lxml")
        if _seen is not None and not system_board:
            _seen["full_list"] = _full_list_link(soup, final)
        if follow:
            evidence = [(final, _LOADED)] if final != url else []
            evidence += _static_evidence(html, soup, final)
            cands = _candidates(evidence, url)
            own_breezy = _breezy_own_domain(html, soup, final)
            if own_breezy is not None:
                cands.insert(0, (own_breezy, _LOADED, 1))
            # ---- 2. a job board the page loads or embeds
            hit, empty = _try_boards(cands, tried, True, reader_extra)
            if hit:
                return finish(hit[1], _how_board(hit[0], hit[2]), board=hit[0])
            empty_board = empty_board or empty

        # ---- 3. structured job data
        jobs = jsonld_jobs(soup, final)
        if jobs:
            return finish(jobs, "read from structured job data on the page")

        # ---- 4. job links on the page, following "next page" links
        try:
            jobs, pages, cut = _static_listing(final, BeautifulSoup(html, "lxml"), rx, trusted, label)
            if jobs:
                how = "read from the jobs listed on the page"
                if pages > 1:
                    how += f" ({pages} pages)"
                return finish(jobs, how, truncated=cut)
        except FetchError as exc:
            notes.append(str(exc))

        m = _SAYS_NONE.search(_visible_text(soup))
        says_none = m.group(0) if m else ""

    def weak_boards(candidates, rendered):
        """A job board the page merely links to or mentions. Tried only after the page's own
        list (as a visitor sees it), and only believed if it has jobs: one stray link is weak
        evidence, and an empty board behind a stray link says nothing about this company."""
        if not follow:
            return None
        hit, _ = _try_boards(candidates, tried, False, reader_extra)
        return finish(hit[1], _how_board(hit[0], hit[2]), board=hit[0], rendered=rendered) if hit else None

    # ---- 5. the same, in a headless browser
    render_problem = ""       # the browser couldn't be used, or the site refused it (not the link's fault)
    page_gone = False         # the browser got a "not found" for the page itself
    untitled = [0]
    if browser is None:
        done = weak_boards(cands, False)
        if done:
            return done
    else:
        found: dict = {}

        def collect(frames) -> int:
            for frame_url, frame_html in frames:
                fsoup = BeautifulSoup(frame_html, "lxml")
                if not system_board:
                    _strip_chrome(fsoup)
                untitled[0] += _read_listing_html(fsoup, frame_url, found, rx)
            return len(found)

        try:
            page = browser.render(final if html else url, collect=collect)
        except BrowserUnavailable as exc:
            render_problem = str(exc)
            done = weak_boards(cands, False)
            if done:
                return done
        else:
            result.final_url = page.final_url or final
            if _seen is not None and not system_board and not _seen.get("full_list") and page.frames:
                _seen["full_list"] = _full_list_link(BeautifulSoup(page.frames[0][1], "lxml"), page.frames[0][0])
            if page.status in (404, 410):
                page_gone = True
            elif page.status in (401, 403, 429) or page.status >= 500:
                render_problem = f"the site refused the browser (HTTP {page.status})"
            elif len(page.text or "") < 2000 and _BLOCKED.search(page.text or "") and not found:
                render_problem = "the site showed the browser a bot check instead of the page"
            else:
                saw_page = True
                m = _SAYS_NONE.search(page.text or "")
                says_none = m.group(0) if m else ""      # what the page shows beats what its source contains
            rcands: list = []
            if follow:      # even on a "not found" status: some sites serve a working app with a 404
                evidence = [(u, _LOADED) for u in page.requests]
                evidence += [(fu, _EMBEDDED) for fu, _ in page.frames[1:]]
                if page.final_url and page.final_url != url:
                    evidence.append((page.final_url, _LOADED))
                for fu, fh in page.frames[:1]:
                    evidence += [(u, st) for u, st in _static_evidence(fh, BeautifulSoup(fh, "lxml"), fu)
                                 if st == _LINKED]
                rcands = _candidates(evidence, url)
                hit, empty = _try_boards(rcands, tried, True, reader_extra)
                if hit:
                    return finish(hit[1], _how_board(hit[0], hit[2]), board=hit[0], rendered=True)
                empty_board = empty_board or empty
            for fu, fh in page.frames:
                jobs = jsonld_jobs(BeautifulSoup(fh, "lxml"), fu)
                if jobs:
                    return finish(jobs, "read from structured job data on the page (in a browser)", rendered=True)
            try:
                jobs = _found_to_jobs(found, trusted, f"on the {label}")
                if jobs:
                    how = "read from the jobs the page shows in a browser"
                    if page.clicks:
                        how += f" ({page.clicks + 1} pages of results)"
                    return finish(jobs, how, truncated=page.stopped, rendered=True)
                if untitled[0]:
                    notes.append(f"found {untitled[0]} job links on the {label} but couldn't read their titles")
            except FetchError as exc:
                notes.append(str(exc))
            cands = cands + rcands
            done = weak_boards(cands, True)
            if done:
                return done

    # ---- 6. a job board the page points at that has no data feed (Paycom, isolved, Gem...):
    #         open that board itself.
    needs_browser = []
    if follow:
        for key, state in list(tried.items()):
            if state[0] != "browser":
                continue
            board, strength = state[1], state[2]
            if browser is None:
                needs_browser.append(board.label())
                continue
            try:
                sub = _read_page(board.listing_url, link_regex=B.RENDERED[board.system]["links"],
                                 browser=browser, follow_boards=False, label=f"{board.system} job board")
            except NeedsLink as exc:
                tried[key] = ("gone", str(exc))
                continue
            except FetchError as exc:
                tried[key] = ("error", str(exc), strength)
                continue
            if sub.jobs:
                return finish(sub.jobs, _how_board(board, strength), board=board,
                              truncated=sub.truncated, rendered=True)
            tried[key] = ("empty", "")
            if strength >= _EMBEDDED and empty_board is None:
                empty_board = (board, [], strength)

    # ---- nothing found. Which of the three outcomes is this?
    def name(key):
        return key.split(":{")[0].rstrip(":")
    # Only a board the page really loads or embeds counts as "the board didn't answer". A stray
    # link to some board that errors must not hold a company in "couldn't check" forever.
    board_down = [f"{name(k)} ({v[1]})" for k, v in tried.items() if v[0] == "error" and v[2] >= _EMBEDDED]
    gone = [f"{name(k)} ({v[1]})" for k, v in tried.items() if v[0] == "gone"]

    # (b) Couldn't check this run: the board the page loads didn't answer.
    if board_down:
        raise FetchError(f"the job board behind the {label} didn't answer: " + "; ".join(board_down[:3]))

    # (a) Nothing open right now. Always on a positive sign, never on mere absence: a board the
    #     page loads that answered with an empty list, or the page's own words.
    if empty_board is not None:
        board = empty_board[0]
        return finish(JobList(), f"the job board this page loads lists no openings ({board.label()})", board=board)
    if says_none and (saw_page or browser is None) and not notes and not page_gone:
        return finish(JobList(), f"the page says nothing is open (\"{says_none.strip()}\")", rendered=saw_page)

    # (b) Couldn't check this run: the browser couldn't be used on a page that needs it.
    if render_problem and html:
        raise FetchError(f"the {label} needs a browser to show its jobs, and the browser couldn't be "
                         f"used this run ({render_problem})")

    # (c) The link needs fixing.
    if not html:
        extra = f"; in a browser: {render_problem or 'not found'}" if (render_problem or page_gone) else ""
        why = f"the {label} doesn't load ({static_error or 'empty page'}{extra})"
        if page_gone or re.search(r"HTTP (404|410)\b", static_error) or not static_error:
            raise NeedsLink(why)
        raise PageDown(why)
    if page_gone:
        raise NeedsLink(f"the {label} doesn't exist any more (not found)")
    found_msg = next((n for n in reversed(notes) if n.startswith("found ")), "")
    if found_msg:
        raise NeedsLink(f"{found_msg}, so nothing was published")
    if gone:
        raise NeedsLink(f"the {label} points at a job board that isn't there: " + "; ".join(gone[:3]))
    weak_empty = [name(k) for k, v in tried.items() if v[0] == "empty"]
    if weak_empty:
        raise NeedsLink(f"the {label} links to a job board that lists nothing ({weak_empty[0]}); "
                        "if that is the company's board, use its link directly")
    if needs_browser:
        raise NeedsLink(f"the {label} points at a job board that needs a browser to read "
                        f"({needs_browser[0]}), and none was used")
    if system_board and saw_page:
        raise NeedsLink(f"the {label} opened, but no postings were recognised on it and it doesn't say "
                        "there are none - its layout may have changed")
    if browser is None:
        raise NeedsLink(f"no jobs are listed on the {label} itself; it may need a browser to show them, "
                        "and none was used - use the job board's link instead")
    raise NeedsLink(f"no jobs were found on the {label}, even after running its scripts - "
                    "if the company is hiring, use the job board's link instead")


def _static_listing(url: str, first_soup, rx, trusted: bool, label: str) -> tuple:
    """Job links on a plain-downloaded page, following its next-page links.
    Returns (jobs, pages_read, cut_short_note)."""
    found: dict = {}
    untitled = 0
    page_url, soup, visited, pages_read, cut = url, first_soup, set(), 0, ""
    while page_url and page_url not in visited:
        if pages_read >= ONPAGE_MAX_PAGES:
            cut = f"stopped after {ONPAGE_MAX_PAGES} pages of results"
            break
        visited.add(page_url)
        if soup is None:
            try:
                r = get(page_url)
            except FetchError:
                break              # a later page failing keeps what was already read
            visited.add(r.url.split("#")[0])
            soup, page_url = BeautifulSoup(r.text, "lxml"), r.url
        pages_read += 1
        # Find the next page BEFORE stripping menus: pagination links usually
        # sit inside a <nav>, which the next line removes.
        next_url = _next_page(soup, page_url)
        if rx is None:
            _strip_chrome(soup)
        before = len(found)
        untitled += _read_listing_html(soup, page_url, found, rx)
        if pages_read > 1 and len(found) == before:
            break                  # a "next" page with nothing new: stop
        page_url, soup = next_url, None

    jobs = _found_to_jobs(found, trusted, f"on the {label}")
    if not jobs and untitled:
        raise FetchError(f"found {untitled} job links on the {label} but couldn't read their titles")
    return jobs, pages_read, cut


def read_rendered_board(board: Board, browser: Optional[Browser]) -> PageResult:
    """Read a job board that has no data feed by opening its own page."""
    return read_page(board.listing_url, link_regex=B.RENDERED[board.system]["links"],
                     browser=browser, follow_boards=False, label=f"{board.system} job board")
