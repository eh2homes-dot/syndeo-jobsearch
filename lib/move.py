"""The single record type every source emits.

Confidence is the important field. The newsletter rule is:

  CONFIRMED  filed or announced by the company itself -> name the person
  PROBABLE   a leadership page changed -> name the person
  INFERRED   a req closed, a cluster appeared -> describe the company, name
             nobody

Never promote a tier because an item looks convincing. These are people Evan
knows and may place; a wrong name costs more than the item is worth.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

CONFIRMED = "confirmed"
PROBABLE = "probable"
INFERRED = "inferred"

TIER_ORDER = {CONFIRMED: 0, PROBABLE: 1, INFERRED: 2}

# Kinds of movement, used for grouping in the report.
ARRIVAL = "arrival"
DEPARTURE = "departure"
PROMOTION = "promotion"
UNKNOWN = "unknown"


@dataclass
class Move:
    company: str
    source: str
    confidence: str
    kind: str = UNKNOWN
    person: Optional[str] = None
    title: Optional[str] = None
    previous_title: Optional[str] = None
    summary: str = ""
    url: str = ""
    observed: str = field(default_factory=lambda: date.today().isoformat())
    published: Optional[str] = None
    score: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def sort_key(self) -> tuple:
        return (TIER_ORDER.get(self.confidence, 9), -self.score, self.company.lower())

    def display(self) -> str:
        """One-line rendering for the report."""
        if self.confidence == INFERRED or not self.person:
            return self.summary or f"{self.company}: activity detected"
        bits = [f"**{self.person}**"]
        if self.title:
            bits.append(f"— {self.title}")
        bits.append(f"at **{self.company}**")
        line = " ".join(bits)
        if self.previous_title:
            line += f" (previously {self.previous_title})"
        return line
