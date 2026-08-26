"""The bot's inbound side: commands, access control, and offsets."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from app.briefing.models import Briefing, BriefingStats, Source, Story
from app.core.config import Settings
from app.delivery.bot import (
    HELP_TEXT,
    BriefingBot,
    Update,
    latest_reply,
    status_reply,
)
from app.intelligence.categories import Category
from app.storage.briefing_store import write_briefing
from app.storage.run_store import RunRecord, write_run

OWNER = "6706372259"
STRANGER = "999999999"
NOW = datetime.now(UTC)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "telegram_bot_token": "123:ABC",
        "telegram_chat_id": OWNER,
        "data_dir": tmp_path,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def briefing(generated_at: datetime = NOW) -> Briefing:
    return Briefing(
        day=date(2026, 8, 26),
        generated_at=generated_at,
        covers_since=generated_at - timedelta(days=1),
        stories=[
            Story(
                event_id="evt_1",
                headline="Gemma 4 released",
                what_happened="It happened.",
                why_it_matters="It matters.",
                category=Category.MODEL_RELEASE,
                score=7.0,
                confidence=0.9,
                sources=[Source(source_id="openai", title="T", url="https://openai.com/a")],
                first_seen=generated_at,
                last_updated=generated_at,
            )
        ],
        stats=BriefingStats(feeds_ok=25, articles=515, events=123, provider="groq"),
    )


def make_bot(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    **overrides: object,
) -> BriefingBot:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []})

    client = httpx.Client(transport=httpx.MockTransport(handler or default_handler))
    return BriefingBot(settings(tmp_path, **overrides), client=client)


# --- replies ------------------------------------------------------------------


def test_any_message_returns_the_latest_briefing(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())
    bot = make_bot(tmp_path)

    reply = bot.handle(Update(update_id=1, chat_id=OWNER, text="what's new"))

    assert reply is not None
    assert "Gemma 4 released" in reply


def test_help_lists_the_commands(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)

    assert bot.handle(Update(update_id=1, chat_id=OWNER, text="/start")) == HELP_TEXT
    assert bot.handle(Update(update_id=2, chat_id=OWNER, text="/help")) == HELP_TEXT


def test_status_reports_the_last_run_and_the_history(tmp_path: Path) -> None:
    """Read from run records, not the briefing, so a failed run is reportable too."""
    write_run(
        tmp_path,
        RunRecord(
            started_at=NOW,
            finished_at=NOW,
            ok=True,
            articles_fetched=515,
            provider="groq:openai/gpt-oss-120b",
            model_calls=25,
            stories_published=5,
            delivered=True,
        ),
    )

    reply = status_reply(tmp_path)

    assert "515" in reply
    assert "groq" in reply
    assert "100%" in reply


def test_status_reports_a_failed_run(tmp_path: Path) -> None:
    """A briefing cannot describe the run that failed to produce it."""
    write_run(
        tmp_path,
        RunRecord(
            started_at=NOW,
            finished_at=NOW,
            ok=False,
            error="LLMError: provider unreachable",
        ),
    )

    reply = status_reply(tmp_path)

    assert "failed" in reply
    assert "provider unreachable" in reply


def test_status_before_any_run_says_so(tmp_path: Path) -> None:
    assert "No runs recorded" in status_reply(tmp_path)


def test_refresh_runs_the_pipeline(tmp_path: Path) -> None:
    calls: list[str] = []

    def refresh() -> str:
        calls.append("ran")
        return "✅ Rebuilt."

    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    )
    bot = BriefingBot(settings(tmp_path), client=client, refresh=refresh)

    reply = bot.handle(Update(update_id=1, chat_id=OWNER, text="/refresh"))

    assert calls == ["ran"]
    assert reply == "✅ Rebuilt."


def test_refresh_without_a_runner_says_so_rather_than_failing(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)

    reply = bot.handle(Update(update_id=1, chat_id=OWNER, text="/refresh"))

    assert reply is not None
    assert "not available" in reply


def test_asking_before_the_first_run_explains_what_to_do(tmp_path: Path) -> None:
    reply = latest_reply(tmp_path)

    assert "/refresh" in reply


def test_a_stale_briefing_is_labelled(tmp_path: Path) -> None:
    """A briefing served hours later must not pretend to be current."""
    write_briefing(tmp_path, briefing(generated_at=NOW - timedelta(hours=20)))

    reply = latest_reply(tmp_path)

    assert "hours old" in reply
    assert "/refresh" in reply


def test_a_fresh_briefing_carries_no_staleness_note(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing(generated_at=NOW - timedelta(minutes=5)))

    assert "hours old" not in latest_reply(tmp_path)


# --- access control -----------------------------------------------------------


def test_a_stranger_gets_no_reply_at_all(tmp_path: Path) -> None:
    """Silence, not an error: an error confirms the bot exists and is worth probing."""
    write_briefing(tmp_path, briefing())
    bot = make_bot(tmp_path)

    assert bot.handle(Update(update_id=1, chat_id=STRANGER, text="/latest")) is None


def test_a_stranger_cannot_spend_the_model_budget(tmp_path: Path) -> None:
    calls: list[str] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    )

    def refresh() -> str:
        calls.append("ran")
        return "done"

    bot = BriefingBot(settings(tmp_path), client=client, refresh=refresh)

    assert bot.handle(Update(update_id=1, chat_id=STRANGER, text="/refresh")) is None
    assert calls == []


# --- polling ------------------------------------------------------------------


def test_updates_are_parsed_and_acknowledged(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "getUpdates" in str(request.url) and len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 41,
                            "message": {"chat": {"id": int(OWNER)}, "text": "hi"},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    write_briefing(tmp_path, briefing())
    bot = make_bot(tmp_path, handler)

    assert bot.poll_once() == 1
    # The next poll must advance past the handled update, or it repeats forever.
    bot.poll_once()
    assert "offset=42" in str(seen[-1].url)


def test_a_malformed_update_is_skipped_not_crashed_on(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 1},
                    {"update_id": 2, "message": {"chat": {}}},
                    "not even a dict",
                ],
            },
        )

    bot = make_bot(tmp_path, handler)

    assert bot.poll_once() == 0


def test_a_failed_poll_returns_nothing_rather_than_raising(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no network")

    bot = make_bot(tmp_path, handler)

    assert bot.fetch_updates() == []


def test_a_server_error_while_polling_is_survivable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    bot = make_bot(tmp_path, handler)

    assert bot.poll_once() == 0


# --- public read-only mode ----------------------------------------------------


def public(tmp_path: Path, **overrides: object) -> BriefingBot:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []})

    values: dict[str, object] = {"public_read_only": True, "public_reply_seconds": 0}
    values.update(overrides)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BriefingBot(settings(tmp_path, **values), client=client)


def test_a_guest_gets_the_briefing_when_public_mode_is_on(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())
    bot = public(tmp_path)

    reply = bot.handle(Update(update_id=1, chat_id=STRANGER, text="hi"))

    assert reply is not None
    assert "Gemma 4 released" in reply


def test_a_guest_still_cannot_spend_the_model_budget(tmp_path: Path) -> None:
    """The whole point of read-only: opening the bot must not open the wallet."""
    calls: list[str] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    )

    def refresh() -> str:
        calls.append("ran")
        return "done"

    bot = BriefingBot(
        settings(tmp_path, public_read_only=True, public_reply_seconds=0),
        client=client,
        refresh=refresh,
    )

    reply = bot.handle(Update(update_id=1, chat_id=STRANGER, text="/refresh"))

    assert calls == []
    assert reply is not None
    assert "owner" in reply


def test_a_guest_cannot_read_run_internals(tmp_path: Path) -> None:
    bot = public(tmp_path)

    reply = bot.handle(Update(update_id=1, chat_id=STRANGER, text="/status"))

    assert reply is not None
    assert "owner" in reply


def test_a_guest_help_does_not_advertise_owner_commands(tmp_path: Path) -> None:
    bot = public(tmp_path)

    reply = bot.handle(Update(update_id=1, chat_id=STRANGER, text="/start"))

    assert reply is not None
    assert "/refresh" not in reply
    assert "github.io" in reply


def test_a_guest_is_rate_limited(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())
    bot = public(tmp_path, public_reply_seconds=60)

    first = bot.handle(Update(update_id=1, chat_id=STRANGER, text="hi"))
    second = bot.handle(Update(update_id=2, chat_id=STRANGER, text="hi again"))

    assert first is not None
    assert second is None


def test_the_rate_limit_is_per_chat(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())
    bot = public(tmp_path, public_reply_seconds=60)

    bot.handle(Update(update_id=1, chat_id=STRANGER, text="hi"))
    other = bot.handle(Update(update_id=2, chat_id="123123123", text="hi"))

    assert other is not None


def test_the_owner_is_not_rate_limited(tmp_path: Path) -> None:
    write_briefing(tmp_path, briefing())
    bot = public(tmp_path, public_reply_seconds=60)

    first = bot.handle(Update(update_id=1, chat_id=OWNER, text="hi"))
    second = bot.handle(Update(update_id=2, chat_id=OWNER, text="hi"))

    assert first is not None
    assert second is not None


def test_public_mode_is_off_by_default(tmp_path: Path) -> None:
    """Opening the bot has to be a deliberate act, not a default."""
    bot = make_bot(tmp_path)

    assert bot.handle(Update(update_id=1, chat_id=STRANGER, text="hi")) is None


def test_a_reply_goes_to_whoever_asked(tmp_path: Path) -> None:
    """Without the chat override every answer would be sent to the owner, so a guest's
    question would be answered to somebody else."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "getUpdates" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 7, "message": {"chat": {"id": int(STRANGER)}, "text": "hi"}}
                    ],
                },
            )
        sent.append(json.loads(request.content)["chat_id"])
        return httpx.Response(200, json={"ok": True})

    write_briefing(tmp_path, briefing())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    bot = BriefingBot(
        settings(tmp_path, public_read_only=True, public_reply_seconds=0), client=client
    )

    bot.poll_once()

    assert sent == [STRANGER]


def test_drain_confirms_updates_so_they_are_not_answered_twice(tmp_path: Path) -> None:
    """A scheduled run has no next poll to confirm on, so without an explicit confirm
    every run would re-read and re-answer the same messages forever."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "getUpdates" in url:
            calls.append(url)
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {"update_id": 9, "message": {"chat": {"id": int(OWNER)}, "text": "hi"}}
                        ],
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True})

    write_briefing(tmp_path, briefing())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    bot = BriefingBot(settings(tmp_path), client=client)

    assert bot.drain() == 1
    # The confirming call carries an offset past the handled update.
    assert any("offset=10" in url for url in calls)


def test_drain_with_nothing_waiting_confirms_nothing(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "result": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert BriefingBot(settings(tmp_path), client=client).drain() == 0
    assert len(calls) == 1
