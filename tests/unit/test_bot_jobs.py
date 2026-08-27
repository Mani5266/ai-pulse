"""The two bot entry points: the scheduled drain and the long-running process.

These are thin, which is the argument for testing them rather than against it. Both are
the first thing a deployment runs, both decide whether the process lives or dies from a
configuration check, and neither is exercised by any other test — a mistake here fails a
workflow at 02:00 rather than in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.jobs import poll_bot, serve_bot


def settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "telegram_bot_token": "123:ABC",
        "telegram_chat_id": "6706372259",
        "data_dir": tmp_path,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class FakeBot:
    """Stands in for BriefingBot. Records that it was drained and closed."""

    def __init__(self, drained: int = 0, *, fail: bool = False) -> None:
        self.drained = drained
        self.fail = fail
        self.closed = False

    def drain(self) -> int:
        if self.fail:
            raise RuntimeError("telegram is down")
        return self.drained

    def close(self) -> None:
        self.closed = True


# --- poll_bot -------------------------------------------------------------------------


def test_polling_without_credentials_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2, not 0: a workflow that cannot answer must fail visibly, not look healthy."""
    monkeypatch.setattr(
        poll_bot, "get_settings", lambda: settings(tmp_path, telegram_bot_token=None)
    )

    assert poll_bot.main() == 2


def test_polling_drains_the_queue_and_closes_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot(drained=3)
    monkeypatch.setattr(poll_bot, "get_settings", lambda: settings(tmp_path))
    monkeypatch.setattr(poll_bot, "BriefingBot", lambda _settings: bot)

    assert poll_bot.main() == 0
    assert bot.closed is True


def test_a_failed_drain_still_closes_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client is closed in a finally block, so a crash cannot leak the connection."""
    bot = FakeBot(fail=True)
    monkeypatch.setattr(poll_bot, "get_settings", lambda: settings(tmp_path))
    monkeypatch.setattr(poll_bot, "BriefingBot", lambda _settings: bot)

    with pytest.raises(RuntimeError):
        poll_bot.main()

    assert bot.closed is True


# --- serve_bot ------------------------------------------------------------------------


def test_serving_without_credentials_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        serve_bot, "get_settings", lambda: settings(tmp_path, telegram_chat_id=None)
    )

    assert serve_bot.main() == 2


def test_a_successful_refresh_acknowledges_rather_than_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline delivers the briefing itself, so the reply is an acknowledgement."""
    monkeypatch.setattr(serve_bot, "run", lambda _settings: 0)

    reply = serve_bot.refresh_and_report(settings(tmp_path))

    assert "Rebuilt" in reply


def test_a_nonzero_exit_code_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve_bot, "run", lambda _settings: 1)

    reply = serve_bot.refresh_and_report(settings(tmp_path))

    assert "exit code 1" in reply
    assert "/status" in reply


def test_a_crashed_pipeline_answers_instead_of_going_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot that silently does nothing is worse than one that says it failed."""

    def explode(_settings: Settings) -> int:
        raise ValueError("feed registry is unreadable")

    monkeypatch.setattr(serve_bot, "run", explode)

    reply = serve_bot.refresh_and_report(settings(tmp_path))

    assert "ValueError" in reply
    assert "feed registry is unreadable" in reply
