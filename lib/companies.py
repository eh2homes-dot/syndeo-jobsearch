"""Loads the master company list and makes it matchable against free text.

The CSV is exported from the "SYNDEO Real Estate Hiring Leads" sheet. Column
names are matched loosely so a re-export with slightly different headers still
works rather than silently returning zero companies.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Suffixes stripped before matching, so "Evernest Property Management" in the
# sheet still matches a press release that just says "Evernest".
_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|group|holdings|technologies|"
    r"technology|software|systems|partners|properties|property management|"
    r"management|services|realty|real estate|trust|reit)\b\.?",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Column header -> canonical field. Matched by substring, case-insensitive.
_HEADER_MAP = [
    ("company name", "name"),
    ("company", "name"),
    ("website", "website"),
    ("careers page", "careers_url"),
    ("careers", "careers_url"),
    ("leadership", "leadership_url"),
    ("team page", "leadership_url"),
    ("ticker", "ticker"),
    ("cik", "cik"),
    ("state", "state"),
    ("industry segment", "segment"),
    ("segment", "segment"),
    ("hub", "hub"),
    ("alias", "aliases"),
]


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and corporate suffixes, collapse spaces."""
    t = _PUNCT.sub(" ", (text or "").lower())
    t = _SUFFIXES.sub(" ", t)
    return _WS.sub(" ", t).strip()


@dataclass
class Company:
    name: str
    website: str = ""
    careers_url: str = ""
    leadership_url: str = ""
    ticker: str = ""
    cik: str = ""
    state: str = ""
    segment: str = ""
    hub: str = ""
    aliases: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")

    @property
    def match_terms(self) -> list[str]:
        """Normalized strings that, if found in text, indicate this company."""
        terms = {normalize(self.name)}
        terms.update(normalize(a) for a in self.aliases)
        # Very short names ("Side", "Point", "Obie") generate false positives
        # against ordinary English. Drop them from free-text matching; they
        # still work for EDGAR and leadership-page sources, which are keyed on
        # an exact URL or CIK rather than on prose.
        return [t for t in terms if len(t) >= 5]

    def mentioned_in(self, text: str) -> bool:
        haystack = normalize(text)
        return any(f" {t} " in f" {haystack} " for t in self.match_terms)

    def mentioned_outside_names(self, text: str, names) -> bool:
        """Like mentioned_in, but a person's name doesn't count: "Rechat appoints
        Audie Chamberlain" is not news about Chamberlain Group, and "Occams
        Advisory appoints Robin Green" is not news about Robin."""
        own = {normalize(self.name), *(normalize(a) for a in self.aliases)}
        # The full multi-word name spelled out ("Chamberlain Group") is the company.
        if len(self.name.split()) > 1 and re.search(rf"\b{re.escape(self.name)}\b", text, re.I):
            return True
        for name in names or []:
            # The name finder sometimes returns headline words ("Joins Compass");
            # those aren't people, so leave them alone.
            if any(self._PREV_OK.match(w) or self._NEXT_OK.match(w) for w in (name or "").split()):
                continue
            if name and normalize(name) not in own:
                text = re.sub(re.escape(name), " ", text, flags=re.I)
                # Articles go on to use the surname alone ("Chamberlain will lead..."),
                # and a surname can be the company's own name.
                last = name.split()[-1] if len(name.split()) > 1 else ""
                if len(last) >= 4:
                    text = re.sub(rf"\b{re.escape(last)}\b", " ", text, flags=re.I)
        if not self.mentioned_in(text):
            return False
        if self.ambiguous and not self._named_as_itself(text):
            return False
        return True

    # Words that can follow a company name in a headline without making it a
    # different company: "Compass Names...", "Compass CEO...", "Compass, Inc."
    _NEXT_OK = re.compile(
        r"^(?:appoints?|names?|hires?|adds?|taps?|promotes?|welcomes?|announces?|elevates?|"
        r"selects?|picks?|brings?|expands?|launches?|loses?|ceo|cfo|coo|cto|cmo|cro|"
        r"president|chief|founder|co-founder|board|executive|exec|leadership|inc|llc|corp|"
        r"group|holdings|real|homes?|realty|technologies|announcement|to|and|has|is|said|"
        r"says|will|after|as|in|on|for|with)\b", re.I)

    _PREV_OK = re.compile(
        r"^(?:joins?|rejoins?|leaves?|exits?|at|from|of|by|to|for|with|and|former|new|ex|"
        r"named|appointed|hired|says|said|the|after)$", re.I)

    def _named_as_itself(self, text: str) -> bool:
        """For one-word names: at least one mention is the company itself, not
        the first word of another name ("Homeward Health", "Compass Lexecon",
        "Super Micro"). A capitalised word right after it means another name."""
        word = self.name.split()[0]
        for m in re.finditer(rf"\b{re.escape(word)}\b", text, flags=re.I):
            # A capitalised word right before it, with no punctuation between,
            # also means another name ("Analysys Mason", "Focal Point").
            before = re.search(r"([A-Za-z][\w.&-]*)[ \t]+$", text[:m.start()])
            if before and before.group(1)[0].isupper() and not self._PREV_OK.match(before.group(1)):
                continue
            rest = text[m.end():]
            nxt = re.match(r"[\s’'s]*([A-Za-z][\w.&-]*)", rest)
            if not nxt or not nxt.group(1)[0].isupper() or self._NEXT_OK.match(nxt.group(1)):
                return True
            if nxt.group(1).lower() in normalize(self.name).split():
                return True
        return False

    @property
    def ambiguous(self) -> bool:
        """One-word names ("Compass", "Super", "Point", "Mason") are ordinary
        words or belong to other companies too, so news searches add
        real-estate context for them."""
        return len(normalize(self.name).split()) <= 1


def _canonical_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in fieldnames or []:
        low = (raw or "").strip().lower()
        for needle, canon in _HEADER_MAP:
            if needle in low and canon not in mapping.values():
                mapping[raw] = canon
                break
    return mapping


def load(path: str | Path) -> list[Company]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Export the master sheet to CSV and save it there "
            f"(see people-moves/README.md)."
        )

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = _canonical_headers(reader.fieldnames or [])
        if "name" not in headers.values():
            raise ValueError(
                f"{path} has no recognisable company-name column. "
                f"Found: {reader.fieldnames}"
            )

        companies: list[Company] = []
        seen: set[str] = set()

        for row in reader:
            data = {canon: (row.get(raw) or "").strip() for raw, canon in headers.items()}
            name = data.get("name", "")
            if not name:
                continue

            key = normalize(name)
            if key in seen:
                # The sheet has genuine duplicates across tabs; first row wins.
                log.debug("skipping duplicate company: %s", name)
                continue
            seen.add(key)

            aliases = [a.strip() for a in data.pop("aliases", "").split(";") if a.strip()]
            companies.append(Company(aliases=aliases, **data))

    log.info("loaded %d companies from %s", len(companies), path)
    return companies


def _read_source(source: str | Path) -> tuple[str, list[dict]]:
    """Rows from a CSV file, or from a published Google Sheet CSV link."""
    src = str(source)
    if src.startswith(("http://", "https://")):
        import io
        import requests
        r = requests.get(src, timeout=30)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
        return src.split("?")[0], list(csv.DictReader(io.StringIO(text)))
    path = Path(src)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return str(path), list(csv.DictReader(fh))


def _rows_to_companies(label: str, rows: list[dict]) -> list[Company]:
    if not rows:
        return []
    headers = _canonical_headers(list(rows[0].keys()))
    if "name" not in headers.values():
        raise ValueError(f"{label} has no recognisable company-name column. Found: {list(rows[0].keys())}")
    out = []
    for row in rows:
        data = {canon: (row.get(raw) or "").strip() for raw, canon in headers.items()}
        if not data.get("name"):
            continue
        aliases = [a.strip() for a in data.pop("aliases", "").split(";") if a.strip()]
        out.append(Company(aliases=aliases, **data))
    return out


def load_lists(sources: list[str | tuple[str, str]], enrich: str | Path | None = None) -> list[Company]:
    """The company universe: every company on the given lists, once each.

    `sources` are tried in order; an entry may be a (primary, fallback) pair,
    e.g. the live OpCo sheet with opco.csv in the repo as the fallback. The
    lists decide WHO is watched. `enrich` (config/companies.csv) only adds
    details to companies already on a list: ticker, SEC CIK, leadership page,
    aliases. A company that is only in the enrichment file is not watched.
    """
    companies: list[Company] = []
    seen: dict[str, Company] = {}
    for source in sources:
        options = source if isinstance(source, tuple) else (source,)
        parsed, label = None, ""
        for option in options:
            if not option:
                continue
            try:
                label, rows = _read_source(option)
                # A sheet link can answer 200 with a sign-in page or nothing at
                # all; treat that like an outage and use the next option.
                parsed = _rows_to_companies(label, rows)
                if not parsed:
                    raise ValueError("no companies in it")
                break
            except Exception as exc:  # noqa: BLE001 - fall through to the next option
                log.warning("could not read company list %s (%s)", option, exc)
                parsed = None
        if parsed is None:
            raise FileNotFoundError(f"none of these company lists could be read: {options}")
        added = 0
        for c in parsed:
            key = normalize(c.name)
            if not key or key in seen:
                continue
            seen[key] = c
            companies.append(c)
            added += 1
        log.info("loaded %d companies from %s", added, label)

    if enrich and Path(enrich).exists():
        label, rows = _read_source(enrich)
        extra = {normalize(c.name): c for c in _rows_to_companies(label, rows)}
        for c in companies:
            e = extra.get(normalize(c.name))
            if e is None:
                for alias in c.aliases:
                    e = extra.get(normalize(alias))
                    if e:
                        break
            if e is None:
                continue
            for f in ("ticker", "cik", "leadership_url", "website", "careers_url"):
                if not getattr(c, f) and getattr(e, f):
                    setattr(c, f, getattr(e, f))
            c.aliases = sorted(set(c.aliases) | set(e.aliases))
        log.info("enriched from %s: %d with a ticker or CIK, %d with a leadership page",
                 label, len(public_companies(companies)), len(with_leadership_page(companies)))
    return companies


def public_companies(companies: list[Company]) -> list[Company]:
    return [c for c in companies if c.ticker or c.cik]


def with_leadership_page(companies: list[Company]) -> list[Company]:
    return [c for c in companies if c.leadership_url]
