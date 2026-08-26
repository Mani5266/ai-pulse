"""Tests for the pipeline entrypoint."""

from __future__ import annotations

from app.core.config import Settings
from app.jobs.daily_briefing import run


def test_run_returns_success_exit_code() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert run(settings) == 0
