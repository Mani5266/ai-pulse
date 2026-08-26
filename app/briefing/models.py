"""The briefing: the pipeline's output, as data.

A briefing is built once as a structured record and then rendered twice — to Telegram and
to HTML. Rendering from shared data rather than formatting twice is what keeps the two
outputs from drifting apart, and it is why the P8 timeline can re-render history without
re-running the model.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.intelligence.categories import Category
from app.intelligence.verification import VerificationStatus


class Source(BaseModel):
    """One article backing a story. Every claim in a briefing traces to one of these."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    title: str
    url: str


class Claim(BaseModel):
    """One checkable assertion behind a story, and how well the sources back it."""

    model_config = ConfigDict(frozen=True)

    text: str
    status: VerificationStatus
    supported_by: list[str] = Field(default_factory=list)
    contradicted_by: list[str] = Field(default_factory=list)


class Story(BaseModel):
    """One item in the briefing."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    headline: str
    what_happened: str
    why_it_matters: str
    developer_impact: str | None = None
    category: Category
    score: float
    confidence: float
    sources: list[Source] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    first_seen: datetime
    last_updated: datetime

    @property
    def is_developing(self) -> bool:
        """True when this story started on an earlier day.

        The distinction the product exists to make: an announcement that has moved is not
        the same as one that is new, and the briefing says which it is.
        """
        return self.first_seen.date() < self.last_updated.date()

    @property
    def source_count(self) -> int:
        return len({source.source_id for source in self.sources})

    @property
    def contradicted_claims(self) -> list[Claim]:
        """Claims the sources disagree about. The most useful thing to surface."""
        return [claim for claim in self.claims if claim.status is VerificationStatus.CONTRADICTED]

    @property
    def verified_claim_count(self) -> int:
        return sum(1 for claim in self.claims if claim.status is VerificationStatus.VERIFIED)


class BriefingStats(BaseModel):
    """What the run did to produce this. Rendered as the footer, and the P10 dashboard."""

    model_config = ConfigDict(frozen=True)

    feeds_ok: int = 0
    feeds_failed: int = 0
    articles: int = 0
    duplicates_removed: int = 0
    events: int = 0
    events_shortlisted: int = 0
    model_calls: int = 0
    model_failures: int = 0
    provider: str = "none"
    runtime_seconds: float = 0.0


class Briefing(BaseModel):
    """One day's briefing."""

    model_config = ConfigDict(frozen=True)

    day: date
    generated_at: datetime
    covers_since: datetime | None = None
    """Start of the window this briefing reports on.

    A briefing that says "today" while covering four days is lying to its reader, so the
    window it actually covered is carried with it and printed."""

    stories: list[Story] = Field(default_factory=list)
    stats: BriefingStats = Field(default_factory=BriefingStats)

    @property
    def is_empty(self) -> bool:
        return not self.stories

    @property
    def lead(self) -> Story | None:
        """The biggest story, or None on a day where nothing could be summarised."""
        return self.stories[0] if self.stories else None
