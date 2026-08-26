"""Run records and the health figures computed from them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.briefing.render_html import render_stats_page
from app.storage.run_store import (
    FeedOutcome,
    RunRecord,
    compute_health,
    read_runs,
    runs_path,
    write_run,
)

NOW = datetime(2026, 8, 26, 2, 0, tzinfo=UTC)


def record(
    *,
    started: datetime = NOW,
    ok: bool = True,
    articles: int = 500,
    events: int = 120,
    stories: int = 5,
    duration: float = 120.0,
    delivered: bool = True,
    feeds: list[FeedOutcome] | None = None,
    error: str | None = None,
) -> RunRecord:
    return RunRecord(
        started_at=started,
        finished_at=started + timedelta(seconds=duration),
        ok=ok,
        duration_seconds=duration,
        feeds=feeds if feeds is not None else [FeedOutcome(source_id="openai", ok=True)],
        articles_fetched=articles,
        events_touched=events,
        stories_published=stories,
        delivered=delivered,
        model_calls=25,
        error=error,
    )


# --- persistence --------------------------------------------------------------


def test_a_record_round_trips(tmp_path: Path) -> None:
    write_run(tmp_path, record())

    stored = read_runs(tmp_path)

    assert len(stored) == 1
    assert stored[0].articles_fetched == 500
    assert stored[0].ok is True


def test_records_are_grouped_by_month(tmp_path: Path) -> None:
    """A year of daily runs is twelve files, not 365."""
    write_run(tmp_path, record())

    assert runs_path(tmp_path, date(2026, 8, 26)).name == "2026-08.ndjson"


def test_runs_come_back_newest_first(tmp_path: Path) -> None:
    write_run(tmp_path, record(started=NOW - timedelta(days=2)))
    write_run(tmp_path, record(started=NOW))

    assert read_runs(tmp_path)[0].started_at == NOW


def test_a_failed_run_is_recorded_too(tmp_path: Path) -> None:
    """The more useful of the two: a briefing cannot describe the run that failed to
    produce it."""
    write_run(tmp_path, record(ok=False, stories=0, delivered=False, error="LLMError: down"))

    stored = read_runs(tmp_path)[0]

    assert stored.ok is False
    assert stored.error is not None
    assert "down" in stored.error


def test_a_corrupt_line_does_not_lose_the_history(tmp_path: Path) -> None:
    write_run(tmp_path, record())
    with runs_path(tmp_path, date(2026, 8, 26)).open("a", encoding="utf-8") as handle:
        handle.write("{ not json\n")
    write_run(tmp_path, record(started=NOW + timedelta(hours=1)))

    assert len(read_runs(tmp_path)) == 2


def test_no_runs_yet_is_an_empty_list(tmp_path: Path) -> None:
    assert read_runs(tmp_path) == []


def test_the_limit_is_respected(tmp_path: Path) -> None:
    for index in range(5):
        write_run(tmp_path, record(started=NOW + timedelta(hours=index)))

    assert len(read_runs(tmp_path, limit=3)) == 3


# --- health -------------------------------------------------------------------


def test_the_success_rate_is_the_reliability_target() -> None:
    records = [record() for _ in range(19)] + [record(ok=False)]

    health = compute_health(records)

    assert health.success_rate == 0.95
    assert health.meets_reliability_target is True


def test_falling_below_the_target_is_reported_as_such() -> None:
    """A dashboard that can only display good news is decoration."""
    records = [record() for _ in range(8)] + [record(ok=False), record(ok=False)]

    assert compute_health(records).meets_reliability_target is False


def test_typical_figures_use_the_median_not_the_mean() -> None:
    """One catastrophic run should not move the number describing a typical day."""
    records = [record(articles=500) for _ in range(9)] + [record(articles=0, ok=False)]

    health = compute_health(records)

    assert health.median_articles == 500


def test_repeatedly_failing_feeds_are_surfaced() -> None:
    failing = [
        FeedOutcome(source_id="the-rundown", ok=False, error="HTTP 403"),
        FeedOutcome(source_id="openai", ok=True),
    ]
    records = [record(feeds=failing) for _ in range(3)]

    health = compute_health(records)

    assert health.failing_feeds == {"the-rundown": 3}


def test_the_worst_feed_is_listed_first() -> None:
    records = [
        record(
            feeds=[
                FeedOutcome(source_id="often", ok=False),
                FeedOutcome(source_id="rarely", ok=index == 0),
            ]
        )
        for index in range(3)
    ]

    assert list(compute_health(records).failing_feeds) == ["often", "rarely"]


def test_health_of_no_runs_is_zeroed_not_an_error() -> None:
    health = compute_health([])

    assert health.runs == 0
    assert health.success_rate == 0.0
    assert health.meets_reliability_target is False


# --- the published page -------------------------------------------------------


def test_the_stats_page_shows_the_reliability_figure() -> None:
    records = [record() for _ in range(10)]

    page = render_stats_page(compute_health(records), records)

    assert "100%" in page
    assert "target 95%, met" in page


def test_the_stats_page_admits_a_missed_target() -> None:
    records = [record() for _ in range(5)] + [record(ok=False) for _ in range(5)]

    page = render_stats_page(compute_health(records), records)

    assert "not met" in page
    assert "failed" in page


def test_the_stats_page_names_failing_feeds() -> None:
    records = [record(feeds=[FeedOutcome(source_id="the-rundown", ok=False)])]

    page = render_stats_page(compute_health(records), records)

    assert "the-rundown" in page


def test_the_stats_page_survives_an_empty_history() -> None:
    page = render_stats_page(compute_health([]), [])

    assert "0 recorded runs" in page


def test_new_and_ranked_events_are_recorded_separately() -> None:
    """A run can touch nothing and still publish, because the briefing reports 36 hours
    rather than the last run. Without both numbers the funnel reads as a bug."""
    stored = RunRecord(
        started_at=NOW,
        finished_at=NOW,
        ok=True,
        articles_fetched=550,
        events_touched=0,
        events_ranked=121,
        stories_published=5,
    )

    page = render_stats_page(compute_health([stored]), [stored])

    assert "121" in page
    assert "ranked" in page
