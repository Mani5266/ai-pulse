"""Core domain models.

These are the records the pipeline passes between stages and persists as NDJSON.
Fields that later phases populate are optional here, so a P1 record and a P4 record
can live in the same file without a migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.intelligence.categories import Category


class SourceTier(StrEnum):
    """Where a source sits in the credibility hierarchy.

    The tier drives both the deterministic credibility score and the per-tier daily
    caps that stop high-volume feeds from drowning everything else.
    """

    PRIMARY = "primary"
    """First-party announcements: the lab or company itself."""

    RESEARCH = "research"
    """Preprint servers and paper feeds. High volume, needs a cap."""

    JOURNALISM = "journalism"
    """Reporting outlets."""

    ECOSYSTEM = "ecosystem"
    """Tooling, frameworks, infrastructure, practitioner blogs."""


class Source(BaseModel):
    """One RSS feed in the registry."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    tier: SourceTier
    feed_url: HttpUrl
    site_url: HttpUrl | None = None
    credibility: float = Field(ge=0.0, le=1.0)
    enabled: bool = True
    max_items_per_run: int = Field(default=50, ge=1, le=500)
    note: str | None = None
    """Why a source is disabled, or anything a future reader needs to know."""


class Article(BaseModel):
    """One item from one feed.

    ``id`` is assigned in P2 from the canonical URL; until then it is None and records
    are keyed by ``url``.
    """

    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    title: str = Field(min_length=1)
    source_id: str
    fetched_at: datetime
    published_at: datetime | None = None
    summary: str | None = None
    content: str | None = None

    # Populated in P2.
    id: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None

    # Populated in P3.
    event_id: str | None = None


class FeedResult(BaseModel):
    """Outcome of fetching and parsing one feed.

    A failure is a value, not an exception, so one dead feed cannot end the run and
    every outcome lands in the run statistics.
    """

    source_id: str
    ok: bool
    articles: list[Article] = Field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    duration_seconds: float | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def article_count(self) -> int:
        return len(self.articles)


class Event(BaseModel):
    """One real-world development, and every article covering it.

    This, not the article, is the unit the rest of the pipeline reasons about. Four
    outlets writing about one model release is one event with four sources, which is both
    what the briefing should say and a signal that the release matters.

    ``first_seen`` and ``last_updated`` are what make the timeline in P8 possible: an
    event announced on Monday and shipped on Wednesday is one record with two dates, not
    two unrelated stories.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    canonical_title: str = Field(min_length=1)
    category: Category
    entities: list[str] = Field(default_factory=list)
    article_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    first_seen: datetime
    last_updated: datetime

    # Populated in P4.
    importance_score: float | None = None

    # Populated in P5.
    description: str | None = None
    confidence: float | None = None

    @property
    def article_count(self) -> int:
        return len(self.article_ids)

    @property
    def source_count(self) -> int:
        """Independent sources covering the event. A corroboration signal for ranking."""
        return len(self.source_ids)
