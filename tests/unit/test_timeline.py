"""Event timelines reconstructed from committed snapshots."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.core.models import Event
from app.intelligence.categories import Category
from app.intelligence.timeline import build_timelines, developing_timelines
from app.storage.event_store import write_events

MON, TUE, WED = date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)
DAYS = [MON, TUE, WED]


def event(
    *,
    event_id: str = "evt_1",
    title: str = "Gemma 4 released",
    articles: int = 1,
    sources: list[str] | None = None,
    score: float | None = 7.0,
    day: date = MON,
) -> Event:
    when = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
    return Event(
        id=event_id,
        canonical_title=title,
        category=Category.MODEL_RELEASE,
        entities=["model:gemma-4"],
        article_ids=[f"a{index}" for index in range(articles)],
        source_ids=sources or ["google-deepmind"],
        first_seen=datetime(MON.year, MON.month, MON.day, 9, 0, tzinfo=UTC),
        last_updated=when,
        importance_score=score,
    )


# --- reconstruction -----------------------------------------------------------


def test_a_story_reported_once_has_a_single_entry(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event()])

    timelines = build_timelines(tmp_path, DAYS)

    assert len(timelines["evt_1"].entries) == 1
    assert timelines["evt_1"].entries[0].is_first is True
    assert timelines["evt_1"].is_developing is False


def test_a_story_that_grows_records_each_development(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event(articles=1, sources=["google-deepmind"])])
    write_events(tmp_path, WED, [event(articles=3, sources=["google-deepmind", "ollama"], day=WED)])

    timeline = build_timelines(tmp_path, DAYS)["evt_1"]

    assert len(timeline.entries) == 2
    assert timeline.is_developing is True
    assert timeline.entries[1].articles_added == 2
    assert timeline.entries[1].sources_added == ["ollama"]


def test_a_day_where_nothing_moved_is_omitted(tmp_path: Path) -> None:
    """A run touches an event whenever its articles are re-seen. Listing those would make
    the timeline mostly heartbeats."""
    write_events(tmp_path, MON, [event()])
    write_events(tmp_path, TUE, [event(day=TUE)])
    write_events(tmp_path, WED, [event(articles=2, day=WED)])

    timeline = build_timelines(tmp_path, DAYS)["evt_1"]

    assert [entry.day for entry in timeline.entries] == [MON, WED]


def test_a_new_source_is_flagged_as_corroboration(tmp_path: Path) -> None:
    """The most meaningful thing that can happen to a story after it breaks."""
    write_events(tmp_path, MON, [event(sources=["google-deepmind"])])
    write_events(tmp_path, WED, [event(sources=["google-deepmind", "the-verge-ai"], day=WED)])

    entries = build_timelines(tmp_path, DAYS)["evt_1"].entries

    assert entries[0].is_corroboration is False  # first sighting is not corroboration
    assert entries[1].is_corroboration is True


def test_a_score_change_alone_counts_as_a_development(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event(score=6.0)])
    write_events(tmp_path, WED, [event(score=8.5, day=WED)])

    entries = build_timelines(tmp_path, DAYS)["evt_1"].entries

    assert len(entries) == 2
    assert entries[1].importance_score == 8.5


def test_a_retitled_story_counts_as_a_development(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event(title="Gemma 4 announced")])
    write_events(tmp_path, WED, [event(title="Gemma 4 now on Hugging Face", day=WED)])

    entries = build_timelines(tmp_path, DAYS)["evt_1"].entries

    assert entries[1].title == "Gemma 4 now on Hugging Face"


def test_the_latest_snapshot_of_a_day_wins(tmp_path: Path) -> None:
    """A re-run appends rather than rewriting, so the last snapshot is the truth."""
    write_events(tmp_path, MON, [event(articles=1)])
    write_events(tmp_path, MON, [event(articles=4)])

    assert build_timelines(tmp_path, DAYS)["evt_1"].entries[0].article_count == 4


def test_days_running_spans_first_to_last(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event()])
    write_events(tmp_path, WED, [event(articles=2, day=WED)])

    assert build_timelines(tmp_path, DAYS)["evt_1"].days_running == 3


def test_several_events_are_tracked_independently(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event(event_id="evt_a"), event(event_id="evt_b")])
    write_events(tmp_path, WED, [event(event_id="evt_a", articles=5, day=WED)])

    timelines = build_timelines(tmp_path, DAYS)

    assert len(timelines["evt_a"].entries) == 2
    assert len(timelines["evt_b"].entries) == 1


def test_an_empty_store_yields_no_timelines(tmp_path: Path) -> None:
    assert build_timelines(tmp_path, DAYS) == {}


# --- the developing index -----------------------------------------------------


def test_only_stories_that_moved_twice_are_developing(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event(event_id="evt_static"), event(event_id="evt_moving")])
    write_events(tmp_path, WED, [event(event_id="evt_moving", articles=3, day=WED)])

    developing = developing_timelines(build_timelines(tmp_path, DAYS).values())

    assert [timeline.event_id for timeline in developing] == ["evt_moving"]


def test_developing_stories_are_ordered_by_recent_activity(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event(event_id="evt_old"), event(event_id="evt_new")])
    write_events(tmp_path, TUE, [event(event_id="evt_old", articles=2, day=TUE)])
    write_events(tmp_path, WED, [event(event_id="evt_new", articles=2, day=WED)])

    developing = developing_timelines(build_timelines(tmp_path, DAYS).values())

    assert [timeline.event_id for timeline in developing] == ["evt_new", "evt_old"]


def test_nothing_developing_is_an_empty_list_not_an_error(tmp_path: Path) -> None:
    write_events(tmp_path, MON, [event()])

    assert developing_timelines(build_timelines(tmp_path, DAYS).values()) == []
