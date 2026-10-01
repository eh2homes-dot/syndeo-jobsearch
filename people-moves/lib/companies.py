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


def _plain(text: str) -> str:
    """Lowercase and strip punctuation, but keep corporate suffixes."""
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


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

    @property
    def phrase_terms(self) -> list[str]:
        """Full multi-word names whose suffix-stripped form is too short to use.

        "MRI Software" normalizes to "mri", which match_terms drops. The whole
        phrase "mri software" is still distinctive, so match it as written.
        Single-word short names ("Side", "Doma") stay excluded.
        """
        out = set()
        for n in [self.name, *self.aliases]:
            plain = _plain(n)
            if len(normalize(n)) < 5 and len(plain.split()) >= 2:
                out.add(plain)
        return sorted(out)

    def mentioned_in(self, text: str) -> bool:
        haystack = normalize(text)
        if any(f" {t} " in f" {haystack} " for t in self.match_terms):
            return True
        phrases = self.phrase_terms
        if not phrases:
            return False
        plain = f" {_plain(text)} "
        return any(f" {p} " in plain for p in phrases)


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


def public_companies(companies: list[Company]) -> list[Company]:
    return [c for c in companies if c.ticker or c.cik]


def with_leadership_page(companies: list[Company]) -> list[Company]:
    return [c for c in companies if c.leadership_url]
