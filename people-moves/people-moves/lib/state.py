"""Run-to-run state, stored as JSON files committed back to the repo.

GitHub Actions runners are ephemeral, so state has to live somewhere. Committing
it to the repo (rather than using the Actions cache) means:
  - snapshots survive cache eviction
  - `git log state/leadership/evernest.json` is a free audit trail of who
    appeared and disappeared on a company's team page over time
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
SEEN_PATH = STATE_DIR / "seen.json"
SEEN_TTL_DAYS = 120


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s (%s), starting fresh", path, exc)
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Leadership page snapshots
# --------------------------------------------------------------------------

def snapshot_path(slug: str) -> Path:
    return STATE_DIR / "leadership" / f"{slug}.json"


def load_snapshot(slug: str) -> dict:
    return _read_json(snapshot_path(slug), {})


def save_snapshot(slug: str, url: str, people: list[dict], page_hash: str) -> None:
    _write_json(
        snapshot_path(slug),
        {
            "url": url,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "page_hash": page_hash,
            "people": people,
        },
    )


def page_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Seen-item dedupe (news articles, EDGAR filings)
# --------------------------------------------------------------------------

class SeenStore:
    """Remembers item IDs so the same headline never runs two weeks running."""

    def __init__(self) -> None:
        self._data: dict[str, str] = _read_json(SEEN_PATH, {})

    def is_new(self, item_id: str) -> bool:
        return item_id not in self._data

    def mark(self, item_id: str) -> None:
        self._data[item_id] = datetime.now(timezone.utc).date().isoformat()

    def prune(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)).date().isoformat()
        before = len(self._data)
        self._data = {k: v for k, v in self._data.items() if v >= cutoff}
        if before != len(self._data):
            log.info("pruned %d expired seen-ids", before - len(self._data))

    def save(self) -> None:
        self.prune()
        _write_json(SEEN_PATH, self._data)


def item_id(source: str, key: str) -> str:
    return f"{source}:{hashlib.sha1(key.encode('utf-8', 'ignore')).hexdigest()[:16]}"
