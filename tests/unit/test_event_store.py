"""Event store tests: append-only snapshots, latest wins."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.core.models import Event
from app.intelligence.categories import Category
from app.storage.event_store import events_path, latest_events, read_events, write_events

MONDAY = date(2026, 8, 24)
WEDNESDAY = date(2026, 8, 26)


def event(event_id: str, *, articles: int = 1, updated: date = MONDAY) -> Event:
    when = datetime(updated.year, updated.month, updated.day, 9, 0, tzinfo=UTC)
    return Event(
        id=event_id,
        canonical_title=f"Story {event_id}",
        category=Category.MODEL_RELEASE,
        entities=["org:google"],
        article_ids=[f"a{index}" for index in range(articles)],
        source_ids=["google-deepmind"],
        first_seen=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        last_updated=when,
    )


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    written = write_events(tmp_path, MONDAY, [event("evt_a")])

    assert written == 1
    stored = read_events(tmp_path, MONDAY)
    assert len(stored) == 1
    assert stored[0].id == "evt_a"
    assert stored[0].category is Category.MODEL_RELEASE


def test_events_are_partitioned_by_day(tmp_path: Path) -> None:
    write_events(tmp_path, MONDAY, [event("evt_a")])

    assert events_path(tmp_path, MONDAY) == tmp_path / "events" / "2026-08-24.ndjson"


def test_a_later_snapshot_supersedes_an_earlier_one(tmp_path: Path) -> None:
    """The record grows as the story develops; the newest snapshot is the truth."""
    write_events(tmp_path, MONDAY, [event("evt_a", articles=2)])
    write_events(tmp_path, WEDNESDAY, [event("evt_a", articles=5, updated=WEDNESDAY)])

    current = latest_events(tmp_path, [MONDAY, WEDNESDAY])

    assert len(current) == 1
    assert current[0].article_count == 5


def test_history_is_never_rewritten(tmp_path: Path) -> None:
    """Monday's file must still say what Monday knew — that is the timeline."""
    write_events(tmp_path, MONDAY, [event("evt_a", articles=2)])
    write_events(tmp_path, WEDNESDAY, [event("evt_a", articles=5, updated=WEDNESDAY)])

    assert read_events(tmp_path, MONDAY)[0].article_count == 2


def test_rerunning_a_day_converges_instead_of_duplicating(tmp_path: Path) -> None:
    write_events(tmp_path, WEDNESDAY, [event("evt_a", articles=3, updated=WEDNESDAY)])
    write_events(tmp_path, WEDNESDAY, [event("evt_a", articles=4, updated=WEDNESDAY)])

    current = latest_events(tmp_path, [WEDNESDAY])

    assert len(current) == 1
    assert current[0].article_count == 4


def test_latest_events_is_ordered_by_recency(tmp_path: Path) -> None:
    write_events(tmp_path, MONDAY, [event("evt_old")])
    write_events(tmp_path, WEDNESDAY, [event("evt_new", updated=WEDNESDAY)])

    current = latest_events(tmp_path, [MONDAY, WEDNESDAY])

    assert [item.id for item in current] == ["evt_new", "evt_old"]


def test_reading_a_missing_day_is_empty(tmp_path: Path) -> None:
    assert read_events(tmp_path, date(1999, 1, 1)) == []
    assert latest_events(tmp_path, [date(1999, 1, 1)]) == []


def test_writing_nothing_creates_no_file(tmp_path: Path) -> None:
    assert write_events(tmp_path, MONDAY, []) == 0
    assert not events_path(tmp_path, MONDAY).exists()


def test_a_corrupt_line_does_not_lose_the_file(tmp_path: Path) -> None:
    write_events(tmp_path, MONDAY, [event("evt_a")])
    with events_path(tmp_path, MONDAY).open("a", encoding="utf-8") as handle:
        handle.write('{"not":"an event"}\n')
    write_events(tmp_path, MONDAY, [event("evt_b")])

    assert {item.id for item in read_events(tmp_path, MONDAY)} == {"evt_a", "evt_b"}
