"""Telegram delivery and briefing persistence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from app.briefing.models import Briefing, BriefingStats, Source, Story
from app.core.config import Settings
from app.delivery.telegram import TelegramDelivery
from app.intelligence.categories import Category
from app.storage.briefing_store import (
    all_briefings,
    briefing_path,
    build_site,
    read_briefing,
    write_briefing,
)

DAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"_env_file": None}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def configured(**overrides: object) -> Settings:
    return settings(telegram_bot_token="123:ABC", telegram_chat_id="42", **overrides)


def briefing(day: date = DAY, headline: str = "A headline") -> Briefing:
    return Briefing(
        day=day,
        generated_at=NOW,
        stories=[
            Story(
                event_id="evt_1",
                headline=headline,
                what_happened="Something happened.",
                why_it_matters="It matters.",
                category=Category.MODEL_RELEASE,
                score=7.0,
                confidence=0.9,
                sources=[Source(source_id="openai", title="T", url="https://openai.com/a")],
                first_seen=NOW,
                last_updated=NOW,
            )
        ],
        stats=BriefingStats(feeds_ok=22, articles=515, events=490),
    )


# --- delivery -----------------------------------------------------------------


def test_a_message_is_sent_to_the_configured_chat() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = TelegramDelivery(configured(), client=client).send("hello")

    assert result.ok is True
    assert captured["chat_id"] == "42"
    assert captured["text"] == "hello"
    assert captured["parse_mode"] == "HTML"


def test_link_previews_are_disabled() -> None:
    """The briefing links its sources; previews would bury them under thumbnails."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    TelegramDelivery(configured(), client=client).send("hello")

    assert captured["link_preview_options"] == {"is_disabled": True}


def test_missing_configuration_is_reported_not_raised() -> None:
    """A fresh clone runs end to end and simply does not deliver."""
    result = TelegramDelivery(settings()).send("hello")

    assert result.failed
    assert "not configured" in result.detail


def test_a_rejected_message_is_reported_with_the_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"ok":false,"description":"can\'t parse entities"}')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = TelegramDelivery(configured(), client=client).send("<b>broken")

    assert result.failed
    assert "parse entities" in result.detail


def test_a_network_failure_is_a_value_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = TelegramDelivery(configured(), client=client).send("hello")

    assert result.failed
    assert "ConnectTimeout" in result.detail


def test_an_empty_message_is_refused_before_the_api_sees_it() -> None:
    result = TelegramDelivery(configured()).send("   ")

    assert result.failed
    assert "empty" in result.detail


# --- persistence and site -----------------------------------------------------


def test_a_briefing_round_trips(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())

    stored = read_briefing(tmp_path, DAY)

    assert stored is not None
    assert stored.stories[0].headline == "A headline"
    assert stored.stats.articles == 515


def test_a_missing_briefing_reads_as_none(tmp_path: Path) -> None:
    assert read_briefing(tmp_path, date(1999, 1, 1)) is None


def test_briefings_are_stored_one_file_per_day(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())

    assert briefing_path(tmp_path, DAY) == tmp_path / "briefings" / "2026-08-26.json"


def test_a_corrupt_briefing_does_not_break_the_archive(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())
    (tmp_path / "briefings" / "2026-08-25.json").write_text("{ not json", encoding="utf-8")

    assert len(all_briefings(tmp_path)) == 1


def test_the_archive_is_newest_first(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing(date(2026, 8, 24), "Older"))
    write_briefing(tmp_path, briefing(date(2026, 8, 26), "Newer"))

    assert [item.day for item in all_briefings(tmp_path)] == [
        date(2026, 8, 26),
        date(2026, 8, 24),
    ]


def test_the_site_is_rebuilt_from_stored_briefings(tmp_path: Path) -> None:
    """The whole archive regenerates from committed data, with no model calls."""
    data_dir, site_dir = tmp_path / "data", tmp_path / "site"
    write_briefing(data_dir, briefing(date(2026, 8, 24), "Older"))
    write_briefing(data_dir, briefing(date(2026, 8, 26), "Newer"))

    build_site(data_dir, site_dir)

    assert (site_dir / "2026-08-24.html").exists()
    assert (site_dir / "2026-08-26.html").exists()
    assert (site_dir / "archive.html").exists()
    # The landing page is the newest day, so a bare link always shows today.
    assert "Newer" in (site_dir / "index.html").read_text(encoding="utf-8")


def test_building_a_site_with_no_briefings_writes_nothing(tmp_path: Path) -> None:
    assert build_site(tmp_path / "data", tmp_path / "site") == 0
    assert not (tmp_path / "site").exists()


def test_an_empty_run_never_overwrites_a_good_briefing(tmp_path: Path) -> None:
    """Real failure: a re-run three minutes later had an empty ingest window, found no
    events, and replaced a five-story briefing with a blank page."""
    write_briefing(tmp_path, briefing())
    empty = Briefing(day=DAY, generated_at=NOW, stories=[])

    result = write_briefing(tmp_path, empty)

    assert result is None
    stored = read_briefing(tmp_path, DAY)
    assert stored is not None
    assert len(stored.stories) == 1


def test_an_empty_briefing_is_stored_when_there_is_nothing_to_lose(tmp_path: Path) -> None:
    """A genuinely quiet first day should still publish, saying so."""
    result = write_briefing(tmp_path, Briefing(day=DAY, generated_at=NOW, stories=[]))

    assert result is not None
    stored = read_briefing(tmp_path, DAY)
    assert stored is not None
    assert stored.is_empty


def test_a_better_briefing_replaces_an_earlier_one(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing(headline="First attempt"))

    write_briefing(tmp_path, briefing(headline="Second attempt"))

    stored = read_briefing(tmp_path, DAY)
    assert stored is not None
    assert stored.stories[0].headline == "Second attempt"
