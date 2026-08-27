"""Recency windowing.

These tests exist because of a defect that reached the user's phone: the first briefing
was led by a model release from four months earlier. An RSS feed hands over its current
window, and the size of that window is the publisher's choice — 517 articles arrived
spanning thirteen months, and every one was treated as news.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.models import Article
from app.ingestion.recency import filter_recent
from app.storage.state import (
    RunState,
    Window,
    compute_window,
    read_state,
    state_path,
    write_state,
)

NOW = datetime(2026, 8, 26, 7, 0, tzinfo=UTC)


def article(published: datetime | None, *, fetched: datetime = NOW) -> Article:
    return Article(
        url=f"https://example.com/{published or fetched:%Y%m%d%H%M%S}",  # type: ignore[arg-type]
        title="A headline",
        source_id="example",
        fetched_at=fetched,
        published_at=published,
    )


# --- the window ---------------------------------------------------------------


def test_a_first_run_looks_back_a_short_default() -> None:
    window = compute_window(RunState(), now=NOW, first_run_days=2)

    assert window.is_first_run is True
    assert window.start == NOW - timedelta(days=2)


def test_a_later_run_anchors_to_the_last_briefing() -> None:
    """Within the cap, the anchor is the last briefing rather than a fixed clock hour.

    A run can be delayed, run twice, or start late, and the window follows what was
    actually covered rather than assuming a tidy 24 hours passed.
    """
    earlier = NOW - timedelta(hours=20)
    window = compute_window(RunState(last_briefing_at=earlier), now=NOW)

    assert window.start == earlier
    assert window.is_first_run is False
    assert window.hours == 20
    assert window.was_clamped is False


def test_nothing_older_than_a_day_is_ingested_after_a_missed_run() -> None:
    """A daily briefing does not report the day before yesterday.

    This is the ingestion half of the 24-hour rule. Capping only the briefing lookback
    would not hold: articles ingested by this run become *new* events, and new events
    enter the briefing without passing the lookback filter, so a two-day catch-up would
    deliver two-day-old stories as though they had just happened.

    The cost is explicit — news from inside the gap is not reported late, it is not
    reported at all. That is what a hard recency rule buys and what it costs.
    """
    two_days_ago = NOW - timedelta(days=2)
    window = compute_window(RunState(last_briefing_at=two_days_ago), now=NOW)

    assert window.was_clamped is True
    assert window.start == NOW - timedelta(hours=24)
    assert window.hours == 24


def test_a_long_gap_is_clamped() -> None:
    """After a fortnight away, "everything since" is a briefing nobody reads.

    Still parameterised: a deployment that wants a weekly digest rather than a daily
    briefing raises the cap, and the clamp keeps working at whatever value it is given.
    """
    long_ago = NOW - timedelta(days=30)
    window = compute_window(RunState(last_briefing_at=long_ago), now=NOW, max_catchup_days=7)

    assert window.was_clamped is True
    assert window.start == NOW - timedelta(days=7)


def test_the_default_cap_is_one_day() -> None:
    """Pins the promise rather than the plumbing: no story older than 24 hours, ever."""
    from app.core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.max_catchup_days == 1
    assert settings.briefing_lookback_hours == 24


def test_the_window_covers_its_start_inclusively() -> None:
    window = Window(start=NOW - timedelta(days=1), end=NOW)

    assert window.covers(NOW - timedelta(days=1)) is True
    assert window.covers(NOW - timedelta(days=1, seconds=1)) is False


def test_an_article_dated_slightly_in_the_future_is_kept() -> None:
    """A scheduled post or a disagreeing clock; dropping it would lose it permanently,
    because the next run's window starts later still."""
    window = Window(start=NOW - timedelta(days=1), end=NOW)

    assert window.covers(NOW + timedelta(hours=2)) is True


# --- filtering ----------------------------------------------------------------


def test_stale_articles_are_separated_from_fresh_ones() -> None:
    window = Window(start=NOW - timedelta(days=1), end=NOW)
    articles = [
        article(NOW - timedelta(hours=2)),
        article(NOW - timedelta(days=120)),
        article(NOW - timedelta(hours=20)),
    ]

    result = filter_recent(articles, window)

    assert len(result.fresh) == 2
    assert len(result.stale) == 1


def test_the_april_release_that_reached_the_phone_is_now_excluded() -> None:
    """The exact defect: a first briefing led by a four-month-old model release."""
    window = compute_window(RunState(), now=NOW, first_run_days=2)
    april = article(datetime(2026, 4, 2, 9, 0, tzinfo=UTC))

    result = filter_recent([april], window)

    assert result.fresh == []
    assert len(result.stale) == 1


def test_an_undated_article_falls_back_to_when_it_was_fetched() -> None:
    """Dropping undated items would silently lose every feed that omits dates."""
    window = Window(start=NOW - timedelta(days=1), end=NOW)

    result = filter_recent([article(None, fetched=NOW)], window)

    assert len(result.fresh) == 1


def test_an_undated_article_fetched_long_ago_is_stale() -> None:
    window = Window(start=NOW - timedelta(days=1), end=NOW)

    result = filter_recent([article(None, fetched=NOW - timedelta(days=10))], window)

    assert len(result.stale) == 1


def test_stats_describe_the_cut() -> None:
    window = Window(start=NOW - timedelta(days=1), end=NOW)
    articles = [article(NOW)] + [article(NOW - timedelta(days=30 + i)) for i in range(3)]

    stats = filter_recent(articles, window).stats()

    assert stats["input"] == 4
    assert stats["fresh"] == 1
    assert stats["stale"] == 3
    assert stats["fresh_rate"] == 0.25


def test_an_empty_batch_is_handled() -> None:
    result = filter_recent([], Window(start=NOW - timedelta(days=1), end=NOW))

    assert result.fresh == []
    assert result.stats()["fresh_rate"] == 0.0


# --- state --------------------------------------------------------------------


def test_state_round_trips(tmp_path: Path) -> None:
    write_state(tmp_path, RunState(last_briefing_at=NOW, successful_runs=3, total_runs=4))

    state = read_state(tmp_path)

    assert state.last_briefing_at == NOW
    assert state.successful_runs == 3
    assert state.success_rate == 0.75


def test_a_missing_state_file_is_a_first_run(tmp_path: Path) -> None:
    state = read_state(tmp_path)

    assert state.last_briefing_at is None
    assert compute_window(state, now=NOW).is_first_run is True


def test_a_corrupt_state_file_is_a_first_run_not_a_crash(tmp_path: Path) -> None:
    state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state_path(tmp_path).write_text("{ not json", encoding="utf-8")

    assert read_state(tmp_path).last_briefing_at is None


def test_success_rate_of_a_fresh_state_is_zero_not_an_error() -> None:
    assert RunState().success_rate == 0.0
