"""Run records: what each run actually did.

The briefing says what happened in AI. This says what happened in the pipeline, and it
exists because the failure this project is least able to notice is the quiet one. Nobody is
watching at 02:00 UTC. A feed that started returning 403 last Tuesday, a model that has been
failing schema validation on one story every day, a shortlist that has silently been 60%
research for a week — none of those raise an error, and none are visible in a briefing that
still looks perfectly well written.

Every run appends one record, whether it succeeded or not. A record for a failed run is the
more useful of the two, so failure is written before anything is allowed to abort.

Records are grouped by month rather than by day: a year of daily runs is twelve files
instead of 365, and the reliability figure the project claims is a fold over all of them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

RUNS_DIR = "runs"


class FeedOutcome(BaseModel):
    """How one source fared. Kept per run so a slow decline is visible as a pattern."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    ok: bool
    articles: int = 0
    error: str | None = None
    duration_seconds: float | None = None


class RunRecord(BaseModel):
    """One pipeline run, start to finish."""

    model_config = ConfigDict(frozen=True)

    started_at: datetime
    finished_at: datetime
    ok: bool
    duration_seconds: float = 0.0

    # What the run was responsible for.
    window_start: datetime | None = None
    window_hours: float = 0.0
    first_run: bool = False
    window_clamped: bool = False

    # The funnel, stage by stage. These are the numbers that show where a bad briefing
    # went wrong: 500 articles and 2 events is a clustering problem, 500 and 500 is not.
    feeds: list[FeedOutcome] = Field(default_factory=list)
    articles_fetched: int = 0
    articles_in_window: int = 0
    articles_stored: int = 0
    duplicates_removed: int = 0
    events_touched: int = 0
    events_ranked: int = 0
    """Candidates the ranking saw, new and carried together.

    Distinct from ``events_touched``, which counts only what this run created or updated.
    A run can touch nothing and still publish, because the briefing reports the last 36
    hours rather than the last run — and without this number the funnel reads as
    '550 articles, 0 events, 5 stories', which looks like a bug and is not."""

    events_multi_source: int = 0
    events_shortlisted: int = 0
    stories_published: int = 0

    # The model.
    provider: str = "none"
    model_calls: int = 0
    model_failures: int = 0
    model_rate_limited: int = 0
    schema_violations: int = 0
    claims_extracted: int = 0
    claims_corroborated: int = 0

    delivered: bool = False
    delivery_error: str | None = None
    error: str | None = None

    @property
    def day(self) -> date:
        return self.started_at.date()

    @property
    def feeds_ok(self) -> int:
        return sum(1 for feed in self.feeds if feed.ok)

    @property
    def feeds_failed(self) -> list[FeedOutcome]:
        return [feed for feed in self.feeds if not feed.ok]


def runs_path(data_dir: Path, day: date) -> Path:
    """One file per month. A year of daily runs is twelve files, not 365."""
    return data_dir / RUNS_DIR / f"{day.strftime('%Y-%m')}.ndjson"


def write_run(data_dir: Path, record: RunRecord) -> Path:
    """Append one run record. Called for failures as well as successes."""
    path = runs_path(data_dir, record.day)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        record.model_dump(mode="json", exclude_none=True), sort_keys=True, ensure_ascii=False
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    return path


def read_runs(data_dir: Path, *, limit: int | None = None) -> list[RunRecord]:
    """Every stored run, newest first. A corrupt line is skipped, not fatal."""
    directory = data_dir / RUNS_DIR
    if not directory.exists():
        return []

    records: list[RunRecord] = []
    for path in sorted(directory.glob("*.ndjson"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(RunRecord.model_validate_json(stripped))
            except ValidationError as exc:
                logger.warning("%s: skipping unreadable run record: %s", path, exc)

    records.sort(key=lambda record: record.started_at, reverse=True)
    return records[:limit] if limit else records


class Health(BaseModel):
    """The project's own report card, computed from the run history."""

    model_config = ConfigDict(frozen=True)

    runs: int = 0
    successful: int = 0
    delivered: int = 0
    first_run_at: datetime | None = None
    last_run_at: datetime | None = None

    median_articles: int = 0
    median_events: int = 0
    median_stories: int = 0
    median_duration: float = 0.0

    total_model_calls: int = 0
    total_model_failures: int = 0

    failing_feeds: dict[str, int] = Field(default_factory=dict)
    """Source id to the number of recent runs it failed in. A feed that appears here every
    day is dead and the registry has not caught up."""

    @property
    def success_rate(self) -> float:
        """The figure V1 is measured against: at least 95% of days produce a briefing."""
        return self.successful / self.runs if self.runs else 0.0

    @property
    def delivery_rate(self) -> float:
        return self.delivered / self.runs if self.runs else 0.0

    @property
    def meets_reliability_target(self) -> bool:
        return self.success_rate >= 0.95


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def compute_health(records: Sequence[RunRecord]) -> Health:
    """Fold the run history into the numbers worth publishing.

    Medians rather than means: one catastrophic run — a feed outage, a rate-limit storm —
    should not move the number that describes a typical day.
    """
    if not records:
        return Health()

    failing: dict[str, int] = {}
    for record in records:
        for feed in record.feeds_failed:
            failing[feed.source_id] = failing.get(feed.source_id, 0) + 1

    return Health(
        runs=len(records),
        successful=sum(1 for record in records if record.ok),
        delivered=sum(1 for record in records if record.delivered),
        first_run_at=min(record.started_at for record in records),
        last_run_at=max(record.started_at for record in records),
        median_articles=int(_median([record.articles_fetched for record in records])),
        median_events=int(_median([record.events_touched for record in records])),
        median_stories=int(_median([record.stories_published for record in records])),
        median_duration=round(_median([record.duration_seconds for record in records]), 1),
        total_model_calls=sum(record.model_calls for record in records),
        total_model_failures=sum(record.model_failures for record in records),
        failing_feeds=dict(sorted(failing.items(), key=lambda item: -item[1])),
    )
