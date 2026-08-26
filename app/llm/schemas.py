"""Schemas for every model response.

The rule this module enforces: **no model output reaches the rest of the application as
free text**. Every call declares a Pydantic model, the response is validated against it,
and a response that does not validate is discarded rather than parsed leniently.

That is not defensive style for its own sake. The model is being handed untrusted article
text harvested from the open internet, and an article that succeeds in steering it will
produce output that does not fit the schema — wrong shape, extra fields, prose where a
number belongs. Strict validation turns a successful injection into a discarded response
instead of a corrupted briefing.

Every field that feeds the ranking formula is bounded, so a model that returns 9999 for
an impact score fails validation rather than dominating the day's ranking.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCORE_MIN = 0.0
SCORE_MAX = 10.0


class ImpactScores(BaseModel):
    """The three sub-scores the model contributes to the importance formula.

    Together they are 55% of the score; the deterministic half computed in P4 is the
    other 45%. The model is asked only for judgement it is actually better at than a
    keyword rule — how much a development matters, and to whom.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    technical_impact: float = Field(ge=SCORE_MIN, le=SCORE_MAX)
    """How much this changes what is technically possible."""

    industry_impact: float = Field(ge=SCORE_MIN, le=SCORE_MAX)
    """How much this changes the competitive or commercial landscape."""

    developer_impact: float = Field(ge=SCORE_MIN, le=SCORE_MAX)
    """How much this changes what a working developer does on Monday."""

    reasoning: str = Field(min_length=1, max_length=400)
    """One sentence justifying the scores. Kept for the audit trail, never displayed."""


class StoryAnalysis(BaseModel):
    """An editorial summary of one event, for the briefing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: str = Field(min_length=1, max_length=120)
    what_happened: str = Field(min_length=1, max_length=600)
    why_it_matters: str = Field(min_length=1, max_length=600)
    developer_impact: str | None = Field(default=None, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    """The model's confidence that the summary is supported by the supplied articles."""


class EventPair(BaseModel):
    """Whether two events describe the same underlying development.

    Clustering in P3 is deliberately precision-tuned and under-clusters: two outlets
    describing one event in genuinely different words stay two events. This is where that
    is repaired, on a shortlist of candidate pairs, because it is a semantic judgement
    that string overlap provably cannot make — on live data the true and false pairs'
    similarity scores interleaved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    same_event: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=300)


class ExtractedClaim(BaseModel):
    """One factual assertion from a story, with the sources that carry it.

    The model's job here stops at extraction and attribution: what was claimed, and which
    of the supplied documents said it. It is deliberately *not* asked whether the claim is
    true, or how confident to be — that label is computed in code from the attribution,
    for the same reason the ranking formula splits that way. A model asked "is this
    verified?" will answer confidently either way; a model asked "which documents say
    this?" can be checked against the documents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    """The claim, stated plainly. One assertion, not a paragraph."""

    supported_by: list[str] = Field(default_factory=list, max_length=8)
    """Source ids, exactly as they appear in the document tags, that assert this."""

    contradicted_by: list[str] = Field(default_factory=list, max_length=8)
    """Source ids that assert something incompatible with it."""


class ClaimExtraction(BaseModel):
    """Every claim the model found in one event's documents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=8)
