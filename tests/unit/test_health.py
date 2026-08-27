"""Degraded-run detection: what is worth an alert, and what is only a quiet day.

Half of these tests assert that *nothing* is reported. That is deliberate. An alert that
fires on a normal Tuesday is read once, ignored twice, and filtered thereafter, at which
point the real one is invisible too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.delivery.health import SUPERSEDED, assess, format_alert, report_degraded
from app.delivery.telegram import DeliveryResult, TelegramDelivery
from app.storage.run_store import FeedOutcome, RunRecord

NOW = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)


def record(**overrides: object) -> RunRecord:
    """A healthy run: five stories from twenty shortlisted, delivered, no failures."""
    defaults: dict[str, object] = {
        "started_at": NOW,
        "finished_at": NOW,
        "ok": True,
        "articles_in_window": 99,
        "events_shortlisted": 20,
        "stories_published": 5,
        "provider": "chain(groq, openrouter)",
        "model_calls": 25,
        "model_failures": 0,
        "schema_violations": 0,
        "feeds": [FeedOutcome(source_id=f"src-{index}", ok=True) for index in range(24)],
        "delivered": True,
    }
    defaults.update(overrides)
    return RunRecord(**defaults)  # type: ignore[arg-type]


def settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "telegram_bot_token": "123:ABC",
        "telegram_chat_id": "6706372259",
        "data_dir": tmp_path,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class FakeChannel:
    """A TelegramDelivery that records instead of sending."""

    def __init__(self, result: DeliveryResult | None = None) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._result = result or DeliveryResult(ok=True)

    def send(self, message: str, *, chat_id: str | None = None) -> DeliveryResult:
        self.sent.append(message)
        return self._result

    def close(self) -> None:
        self.closed = True


# --- what should stay silent -----------------------------------------------------------


def test_a_healthy_run_reports_nothing() -> None:
    assert assess(record(), min_stories=3) == []


def test_a_quiet_day_is_not_degraded() -> None:
    """Two stories from two shortlisted events is the pipeline working, not failing.

    This is the distinction the whole module exists for: an output is judged against the
    input that was available to it, never against a fixed expectation.
    """
    quiet = record(events_shortlisted=2, stories_published=2)

    assert assess(quiet, min_stories=3) == []


def test_one_dead_feed_is_not_an_incident() -> None:
    """A single rotting source is the weekly feed workflow's business, not an alert's."""
    feeds = [FeedOutcome(source_id=f"src-{index}", ok=index != 0) for index in range(24)]

    assert assess(record(feeds=feeds), min_stories=3) == []


def test_a_superseded_delivery_is_not_a_failure() -> None:
    """The pipeline kept a better briefing and sent nothing on purpose."""
    kept = record(delivered=False, delivery_error=SUPERSEDED)

    assert assess(kept, min_stories=3) == []


# --- what should speak up --------------------------------------------------------------


def test_a_thin_briefing_from_a_full_shortlist_is_reported() -> None:
    thin = record(events_shortlisted=20, stories_published=2)

    codes = [concern.code for concern in assess(thin, min_stories=3)]

    assert codes == ["thin_briefing"]


def test_publishing_nothing_from_a_full_window_is_reported() -> None:
    empty = record(events_shortlisted=0, stories_published=0, articles_in_window=99)

    codes = [concern.code for concern in assess(empty, min_stories=3)]

    assert "nothing_published" in codes


def test_a_run_with_no_model_is_reported_even_though_it_succeeded() -> None:
    """It publishes on the deterministic ranking — correct, and invisible to a reader."""
    degraded = record(provider="none", model_calls=0)

    codes = [concern.code for concern in assess(degraded, min_stories=3)]

    assert "no_model" in codes


def test_half_the_model_calls_failing_is_reported() -> None:
    degraded = record(model_calls=20, model_failures=10)

    codes = [concern.code for concern in assess(degraded, min_stories=3)]

    assert "model_degraded" in codes


def test_a_schema_violation_is_reported_on_its_own() -> None:
    """One is enough: a prompt or a model has changed under the pipeline."""
    codes = [concern.code for concern in assess(record(schema_violations=1), min_stories=3)]

    assert codes == ["schema_violations"]


def test_a_quarter_of_the_registry_failing_is_reported() -> None:
    feeds = [FeedOutcome(source_id=f"src-{index}", ok=index >= 6) for index in range(24)]

    concerns = assess(record(feeds=feeds), min_stories=3)

    assert [concern.code for concern in concerns] == ["feeds_failing"]
    assert "6 of 24" in concerns[0].detail


def test_a_real_delivery_failure_is_reported() -> None:
    failed = record(delivered=False, delivery_error="HTTP 429: Too Many Requests")

    codes = [concern.code for concern in assess(failed, min_stories=3)]

    assert codes == ["delivery_failed"]


# --- the message ------------------------------------------------------------------------


def test_the_alert_escapes_everything_it_interpolates() -> None:
    """Telegram parses HTML, and a source id reaches this text unmodified."""
    feeds = [
        FeedOutcome(source_id="<b>evil</b>" if index < 6 else f"src-{index}", ok=index >= 6)
        for index in range(24)
    ]
    hostile = record(feeds=feeds)

    message = format_alert(hostile, assess(hostile, min_stories=3))

    assert "<b>evil</b>" not in message
    assert "&lt;b&gt;evil&lt;/b&gt;" in message


def test_the_alert_names_the_day_and_where_to_look() -> None:
    thin = record(events_shortlisted=20, stories_published=1)

    message = format_alert(thin, assess(thin, min_stories=3))

    assert "2026-08-27" in message
    assert "/status" in message


# --- sending ----------------------------------------------------------------------------


def test_a_healthy_run_sends_no_message(tmp_path: Path) -> None:
    channel = FakeChannel()

    result = report_degraded(settings(tmp_path), record(), delivery=channel)  # type: ignore[arg-type]

    assert result is None
    assert channel.sent == []


def test_a_degraded_run_sends_one_message(tmp_path: Path) -> None:
    channel = FakeChannel()
    thin = record(events_shortlisted=20, stories_published=1)

    result = report_degraded(settings(tmp_path), thin, delivery=channel)  # type: ignore[arg-type]

    assert result is not None
    assert result.ok is True
    assert len(channel.sent) == 1


def test_alerting_can_be_switched_off(tmp_path: Path) -> None:
    channel = FakeChannel()
    thin = record(events_shortlisted=20, stories_published=1)

    result = report_degraded(
        settings(tmp_path, alert_on_degraded=False),
        thin,
        delivery=channel,  # type: ignore[arg-type]
    )

    assert result is None
    assert channel.sent == []


def test_a_failing_alert_never_breaks_the_run(tmp_path: Path) -> None:
    """The run has already published. An alerting bug must not undo that."""

    class Exploding(TelegramDelivery):
        def __init__(self) -> None:
            self.closed = False

        def send(self, message: str, *, chat_id: str | None = None) -> DeliveryResult:
            raise RuntimeError("socket exploded")

        def close(self) -> None:
            self.closed = True

    thin = record(events_shortlisted=20, stories_published=1)

    result = report_degraded(settings(tmp_path), thin, delivery=Exploding())

    assert result is not None
    assert result.failed is True
    assert "RuntimeError" in result.detail
