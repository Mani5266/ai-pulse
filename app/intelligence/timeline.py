"""Event timelines: how a story changed, day by day.

This is the feature that separates an intelligence system from a newsletter, and it needs
no new data. Every run writes a full snapshot of the events it touched, append-only, one
file per day — so an event's history is already sitting in the repository and can be
reconstructed by reading the snapshots in order. Nothing was designed for this after the
fact; §2.3 chose append-only NDJSON precisely so that this would be possible later.

**A day only appears if something changed.** A snapshot is written whenever a run touches
an event, and a run touches an event whenever any of its articles are re-seen. Listing
every snapshot would produce a timeline of "nothing happened" entries, which is worse than
no timeline. An entry survives only when the article count, the source count, the title or
the score actually moved.

**What changed is computed, never described.** The difference between two snapshots is
arithmetic — two more articles, one new source, the score up by 0.4. No model is asked to
narrate it, because the numbers are already the answer and a model would only add adjectives
to them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import Event
from app.storage.event_store import read_events

logger = logging.getLogger(__name__)


class TimelineEntry(BaseModel):
    """One day in an event's life, and what moved that day."""

    model_config = ConfigDict(frozen=True)

    day: date
    title: str
    article_count: int
    source_count: int
    importance_score: float | None = None

    articles_added: int = 0
    sources_added: list[str] = Field(default_factory=list)
    is_first: bool = False

    @property
    def is_corroboration(self) -> bool:
        """A day another independent outlet picked the story up.

        The most meaningful thing that can happen to a story after it breaks, and the one
        a reader most wants flagged.
        """
        return bool(self.sources_added) and not self.is_first


class Timeline(BaseModel):
    """An event's full history, oldest first."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    title: str
    entries: list[TimelineEntry] = Field(default_factory=list)

    @property
    def days_running(self) -> int:
        if not self.entries:
            return 0
        return (self.entries[-1].day - self.entries[0].day).days + 1

    @property
    def is_developing(self) -> bool:
        """True when the story moved on more than one day."""
        return len(self.entries) > 1

    @property
    def latest(self) -> TimelineEntry | None:
        return self.entries[-1] if self.entries else None

    @property
    def total_sources(self) -> int:
        return self.entries[-1].source_count if self.entries else 0


def _entry(day: date, snapshot: Event, previous: Event | None) -> TimelineEntry | None:
    """Build an entry for one day, or None if nothing changed that day."""
    if previous is None:
        return TimelineEntry(
            day=day,
            title=snapshot.canonical_title,
            article_count=snapshot.article_count,
            source_count=snapshot.source_count,
            importance_score=snapshot.importance_score,
            articles_added=snapshot.article_count,
            sources_added=list(snapshot.source_ids),
            is_first=True,
        )

    articles_added = snapshot.article_count - previous.article_count
    sources_added = [
        source for source in snapshot.source_ids if source not in set(previous.source_ids)
    ]
    title_changed = snapshot.canonical_title != previous.canonical_title
    score_moved = (snapshot.importance_score or 0) != (previous.importance_score or 0)

    if not (articles_added or sources_added or title_changed or score_moved):
        # A run touched the event without anything actually moving. Listing it would make
        # the timeline mostly noise.
        return None

    return TimelineEntry(
        day=day,
        title=snapshot.canonical_title,
        article_count=snapshot.article_count,
        source_count=snapshot.source_count,
        importance_score=snapshot.importance_score,
        articles_added=max(articles_added, 0),
        sources_added=sources_added,
    )


def build_timelines(data_dir: Path, days: Sequence[date]) -> dict[str, Timeline]:
    """Reconstruct every event's history from the daily snapshots.

    ``days`` must run oldest to newest. Within a day the last snapshot of an event wins,
    since a re-run appends rather than rewriting.
    """
    latest_seen: dict[str, Event] = {}
    entries: dict[str, list[TimelineEntry]] = {}
    titles: dict[str, str] = {}

    for day in days:
        snapshots: dict[str, Event] = {}
        for event in read_events(data_dir, day):
            snapshots[event.id] = event

        for event_id, snapshot in snapshots.items():
            entry = _entry(day, snapshot, latest_seen.get(event_id))
            latest_seen[event_id] = snapshot
            titles[event_id] = snapshot.canonical_title
            if entry is not None:
                entries.setdefault(event_id, []).append(entry)

    return {
        event_id: Timeline(event_id=event_id, title=titles[event_id], entries=points)
        for event_id, points in entries.items()
    }


def developing_timelines(timelines: Iterable[Timeline]) -> list[Timeline]:
    """Only the stories that moved on more than one day, most recently active first."""
    developing = [timeline for timeline in timelines if timeline.is_developing]
    developing.sort(
        key=lambda timeline: (timeline.entries[-1].day, len(timeline.entries)), reverse=True
    )
    return developing
