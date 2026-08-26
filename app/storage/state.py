"""Run state: what the last successful run covered.

The briefing answers "what happened since I last told you", and this file is what makes
that sentence true. Without it the pipeline has no idea what it has already reported, and
an RSS feed is no help: a feed returns its *current window*, which for an active news site
is a day and for a quiet engineering blog is a year. The first run of this pipeline ingested
517 articles whose publication dates spanned thirteen months, and published a "daily
briefing" containing releases from April.

Two rules follow.

**Window on the last briefing, never on a fixed 24 hours.** A scheduled run can be delayed,
skipped, or run twice; a machine can be asleep. Anchoring to what was actually covered means
a missed day is picked up rather than lost.

**Cap the catch-up.** After a two-week gap, "everything since" is thousands of articles and
a briefing nobody reads. The window is clamped, and the briefing says what it covers.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"


class RunState(BaseModel):
    """What previous runs achieved. Advanced only on success."""

    model_config = ConfigDict(frozen=True)

    last_briefing_at: datetime | None = None
    """End of the window the last successful briefing covered."""

    last_run_at: datetime | None = None
    """When a run last completed, successful or not. Diagnostics only."""

    successful_runs: int = 0
    total_runs: int = 0

    @property
    def success_rate(self) -> float:
        """The reliability figure the project is measured against."""
        return self.successful_runs / self.total_runs if self.total_runs else 0.0


def state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILE


def read_state(data_dir: Path) -> RunState:
    """Read run state. A missing or corrupt file is a first run, not an error."""
    path = state_path(data_dir)
    if not path.exists():
        return RunState()
    try:
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, OSError) as exc:
        logger.warning("%s: unreadable run state, treating as first run: %s", path, exc)
        return RunState()


def write_state(data_dir: Path, state: RunState) -> Path:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


class Window(BaseModel):
    """The span of time a run is responsible for."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    is_first_run: bool = False
    was_clamped: bool = False
    """True when the gap since the last briefing exceeded the cap."""

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def covers(self, moment: datetime) -> bool:
        """Whether an article published at ``moment`` belongs to this run.

        The end is deliberately not checked. A feed occasionally carries a publication
        timestamp slightly in the future — a scheduled post, or a clock that disagrees —
        and dropping such an item would lose it permanently, since the next run's window
        starts later still.
        """
        return moment >= self.start


def compute_window(
    state: RunState,
    *,
    now: datetime | None = None,
    first_run_days: int = 2,
    max_catchup_days: int = 7,
) -> Window:
    """Work out what this run should cover.

    On a first run there is nothing to anchor to, so a short default is used: enough to
    produce a real briefing, short enough that it is recognisably news rather than an
    archive.
    """
    end = now or datetime.now(UTC)

    if state.last_briefing_at is None:
        return Window(start=end - timedelta(days=first_run_days), end=end, is_first_run=True)

    start = state.last_briefing_at
    cap = end - timedelta(days=max_catchup_days)
    if start < cap:
        logger.warning(
            "last briefing was %.1f days ago; clamping the window to %d days",
            (end - start).total_seconds() / 86400,
            max_catchup_days,
        )
        return Window(start=cap, end=end, was_clamped=True)

    return Window(start=start, end=end)
