"""The published bot feed.

The Worker that reads this file contains no project text and no tests of its own, so every
guarantee it relies on has to be made here: the fields exist, they are named what it looks
for, and nothing in them is a secret.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.briefing.models import Briefing, BriefingStats, Source, Story
from app.delivery.bot_feed import FEED_NAME, build_bot_feed
from app.intelligence.categories import Category
from app.storage.briefing_store import write_briefing

DAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)

# Every field the Worker reads by name. A rename here is a bot that answers nothing.
REQUIRED = {"latest", "generated_at", "day", "help", "owner_only", "status", "no_briefing"}


def briefing(headline: str = "A headline") -> Briefing:
    return Briefing(
        day=DAY,
        generated_at=NOW,
        covers_since=NOW - timedelta(days=1),
        stories=[
            Story(
                event_id="evt_1",
                headline=headline,
                what_happened="Something happened.",
                why_it_matters="It matters.",
                category=Category.MODEL_RELEASE,
                score=8.0,
                confidence=0.9,
                sources=[Source(source_id="src", title="T", url="https://example.com/a")],
                first_seen=NOW,
                last_updated=NOW,
            )
        ],
        stats=BriefingStats(feeds_ok=25, articles=515, events=123, provider="groq"),
    )


def test_nothing_to_publish_writes_nothing(tmp_path: Path) -> None:
    """A fresh clone has no briefing, and an empty feed would make the bot answer nonsense."""
    assert build_bot_feed(tmp_path / "data", tmp_path / "site") is None
    assert not (tmp_path / "site" / FEED_NAME).exists()


def test_the_feed_carries_every_field_the_worker_reads(tmp_path: Path) -> None:
    data, site = tmp_path / "data", tmp_path / "site"
    write_briefing(data, briefing())

    path = build_bot_feed(data, site)

    assert path is not None
    feed = json.loads(path.read_text(encoding="utf-8"))
    assert set(feed) >= REQUIRED
    assert all(feed[field] for field in REQUIRED)


def test_the_briefing_is_rendered_not_summarised(tmp_path: Path) -> None:
    data, site = tmp_path / "data", tmp_path / "site"
    write_briefing(data, briefing("Qwen releases something"))

    path = build_bot_feed(data, site)
    assert path is not None
    feed = json.loads(path.read_text(encoding="utf-8"))

    assert "Qwen releases something" in feed["latest"]
    assert "example.com" in feed["latest"]


def test_the_feed_carries_no_staleness_note(tmp_path: Path) -> None:
    """Written once, read for a day. Only the reader knows how old it has become."""
    data, site = tmp_path / "data", tmp_path / "site"
    write_briefing(data, briefing())

    path = build_bot_feed(data, site)
    assert path is not None
    feed = json.loads(path.read_text(encoding="utf-8"))

    assert "hours old" not in feed["latest"]
    assert feed["generated_at"].startswith("2026-08-27T02:00")


def test_the_feed_is_published_where_the_site_is(tmp_path: Path) -> None:
    """It is fetched over HTTPS from GitHub Pages, so it has to be inside the site."""
    data, site = tmp_path / "data", tmp_path / "site"
    write_briefing(data, briefing())

    path = build_bot_feed(data, site)

    assert path == site / "bot.json"


def test_the_feed_holds_no_credentials(tmp_path: Path) -> None:
    """This file is public. A token reaching it would be a leak with a URL."""
    data, site = tmp_path / "data", tmp_path / "site"
    write_briefing(data, briefing())

    path = build_bot_feed(data, site)
    assert path is not None
    raw = path.read_text(encoding="utf-8")

    # Key prefixes, the Telegram API host whose URLs carry the token in the path, and the
    # settings prefix that would mean configuration had leaked into published output.
    for marker in ("gsk_", "sk-or-", "csk-", "api.telegram.org", "AI_PULSE_"):
        assert marker not in raw
