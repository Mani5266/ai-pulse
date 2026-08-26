"""Event persistence.

Events differ from articles in one important way: an article is written once and never
changes, while an event grows for as long as the story develops. Files stay append-only
anyway, and the reader resolves the current state:

    data/events/2026-08-24.ndjson   evt_a1b2  first_seen Mon, 2 articles
    data/events/2026-08-26.ndjson   evt_a1b2  first_seen Mon, 5 articles

Each day's file holds a full snapshot of the events *touched that day*. Reading a range of
days keeps the last record seen for each id, so the newest snapshot wins. Nothing is ever
rewritten, which keeps the git history honest — the diff for Wednesday shows exactly what
Wednesday knew — and it is precisely this history that the P8 timeline reads back.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from app.core.models import Event

logger = logging.getLogger(__name__)

EVENTS_DIR = "events"


def events_path(data_dir: Path, day: date) -> Path:
    """Path of the event snapshot file for one UTC day."""
    return data_dir / EVENTS_DIR / f"{day.isoformat()}.ndjson"


def read_events(data_dir: Path, day: date) -> list[Event]:
    """Read one day's event snapshots, in file order."""
    path = events_path(data_dir, day)
    if not path.exists():
        return []

    events: list[Event] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(Event.model_validate_json(stripped))
            except ValidationError as exc:
                logger.warning("%s:%d: skipping unreadable event: %s", path, line_number, exc)
    return events


def latest_events(data_dir: Path, days: Iterable[date]) -> list[Event]:
    """Current state of every event touched in the given days.

    ``days`` must be ordered oldest to newest, because the later snapshot of an event
    supersedes the earlier one. Returned newest-updated first.
    """
    current: dict[str, Event] = {}
    for day in days:
        for event in read_events(data_dir, day):
            current[event.id] = event
    return sorted(current.values(), key=lambda event: event.last_updated, reverse=True)


def write_events(data_dir: Path, day: date, events: Sequence[Event]) -> int:
    """Append snapshots of the given events to one day's file.

    Returns the number written. Re-running a day appends a fresh snapshot rather than
    editing the old one; the reader keeps the last, so a re-run converges instead of
    duplicating.
    """
    if not events:
        return 0

    path = events_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        json.dumps(
            event.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            ensure_ascii=False,
        )
        for event in events
    ]

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

    return len(lines)
