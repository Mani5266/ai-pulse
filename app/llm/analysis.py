"""The model stage of the pipeline.

Everything the model is asked to do, in one place, so the total cost of a run is countable
by reading one file:

===================== ============================ ==============================
Call                  Purpose                      Count per run
===================== ============================ ==============================
Impact scoring        3 sub-scores per event       one per shortlisted event
Story analysis        editorial summary            one per briefing story
===================== ============================ ==============================

With a 20-event shortlist and 5 briefing stories that is 25 calls nominally. The
configured budget is 40, the difference being headroom for the single retry each call is
allowed. The provider enforces the ceiling, so the budget is not merely respected by
convention.

Nothing here has authority. The model receives sanitised article text and returns JSON that
is validated before use. Every failure degrades: an event whose scoring call fails keeps
its deterministic score and stays in the ranking, and a story whose summary fails is
dropped from the briefing rather than published unverified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.models import Article, Event
from app.llm.prompts import impact_scoring_prompt, story_analysis_prompt, wrap_documents
from app.llm.provider import BudgetExhaustedError, LLMProvider
from app.llm.schemas import ImpactScores, StoryAnalysis
from app.ranking.scoring import (
    CREDIBILITY_WEIGHT,
    NOVELTY_WEIGHT,
    PERSONAL_WEIGHT,
    ScoredEvent,
)

logger = logging.getLogger(__name__)

TECHNICAL_WEIGHT = 0.20
INDUSTRY_WEIGHT = 0.15
DEVELOPER_WEIGHT = 0.20
IMPACT_WEIGHT = TECHNICAL_WEIGHT + INDUSTRY_WEIGHT + DEVELOPER_WEIGHT

MAX_DOCUMENTS_PER_EVENT = 4
"""Articles shown to the model per event. Four independent accounts are enough to judge a
development, and each additional one costs prompt tokens for less and less."""

SCORING_DOCUMENTS = 3
SCORING_CHARS = 600
"""Scoring needs to know what happened, not to read the article.

Groq's free tier limits tokens per minute, not requests, and the first hosted run proved
the cost of ignoring that: twenty scoring calls at 4,000 characters each exhausted the
per-minute token allowance, and 14 of 25 calls failed. Trimming the scoring prompt cuts
token spend roughly fourfold for no measurable loss — the impact of a release is legible
from its headline and first paragraph."""

SUMMARY_DOCUMENTS = 4
SUMMARY_CHARS = 1500
"""Summarising is the one task that genuinely benefits from more text."""


@dataclass(frozen=True, slots=True)
class AnalysedEvent:
    """A scored event, with whatever the model managed to add."""

    scored: ScoredEvent
    impact: ImpactScores | None = None
    analysis: StoryAnalysis | None = None

    @property
    def event(self) -> Event:
        return self.scored.event

    @property
    def final_score(self) -> float:
        """The complete importance score, or the deterministic half if the model failed.

        When impact scores are missing the deterministic sub-scores are rescaled to fill
        the whole range, so a failed call does not silently push an event to the bottom of
        the ranking. A missing signal must not read as a signal of zero.
        """
        if self.impact is None:
            return self.scored.score

        weighted = (
            self.scored.scores.credibility * CREDIBILITY_WEIGHT
            + self.scored.scores.novelty * NOVELTY_WEIGHT
            + self.scored.scores.personal_relevance * PERSONAL_WEIGHT
            + self.impact.technical_impact * TECHNICAL_WEIGHT
            + self.impact.industry_impact * INDUSTRY_WEIGHT
            + self.impact.developer_impact * DEVELOPER_WEIGHT
        )
        return round(weighted, 3)

    @property
    def model_scored(self) -> bool:
        return self.impact is not None

    def with_score(self) -> Event:
        """The event carrying its final score and summary, ready to persist."""
        update: dict[str, object] = {"importance_score": self.final_score}
        if self.analysis is not None:
            update["description"] = self.analysis.what_happened
            update["confidence"] = self.analysis.confidence
        return self.event.model_copy(update=update)


def _documents_for(
    event: Event,
    articles: dict[str, Article],
    *,
    max_documents: int = MAX_DOCUMENTS_PER_EVENT,
    max_chars: int = SUMMARY_CHARS,
) -> str:
    """Build the untrusted-document block for one event.

    Articles are looked up by id; any that are missing — pruned by retention, or from an
    earlier day — are simply skipped. The event's own title is included as a fallback so
    the model is never handed an empty document set.
    """
    documents: list[tuple[str, str, str | None]] = []
    for article_id in event.article_ids[:max_documents]:
        article = articles.get(article_id)
        if article is None:
            continue
        documents.append((article.source_id, article.title, article.content or article.summary))

    if not documents:
        documents.append(
            (event.source_ids[0] if event.source_ids else "unknown", event.canonical_title, None)
        )

    return wrap_documents(documents, max_chars=max_chars)


def score_impact(
    shortlist: list[ScoredEvent],
    articles: dict[str, Article],
    provider: LLMProvider,
    *,
    reserve: int = 0,
) -> list[AnalysedEvent]:
    """Ask the model for impact scores, one call per event.

    ``reserve`` holds back budget for the summaries that follow. Without it a day of
    retries spends the whole allowance on scoring and the briefing has no prose at all —
    observed on the first live run, where twenty events retrying once each consumed the
    entire forty-call budget and every summary was skipped. Scoring degrades gracefully;
    a briefing with no summaries does not.

    Never raises. An event whose call fails keeps its deterministic score.
    """
    analysed: list[AnalysedEvent] = []
    budget_spent = False

    for item in shortlist:
        if budget_spent or provider.remaining <= reserve:
            if not budget_spent:
                logger.info(
                    "stopping impact scoring at %d events to reserve %d calls for summaries",
                    len(analysed),
                    reserve,
                )
                budget_spent = True
            analysed.append(AnalysedEvent(scored=item))
            continue

        prompt = impact_scoring_prompt(
            _documents_for(
                item.event, articles, max_documents=SCORING_DOCUMENTS, max_chars=SCORING_CHARS
            ),
            item.event.category.value,
        )

        try:
            impact = provider.structured(prompt, ImpactScores)
        except BudgetExhaustedError:
            logger.warning("call budget exhausted after %d events", len(analysed))
            budget_spent = True
            impact = None

        analysed.append(AnalysedEvent(scored=item, impact=impact))

    analysed.sort(
        key=lambda item: (
            -item.final_score,
            -item.event.source_count,
            -item.event.last_updated.timestamp(),
            item.event.id,
        )
    )
    return analysed


def analyse_stories(
    events: list[AnalysedEvent],
    articles: dict[str, Article],
    provider: LLMProvider,
    *,
    limit: int,
) -> list[AnalysedEvent]:
    """Write the editorial summary for the top events.

    A story whose summary call fails is returned without one; the briefing stage drops it
    rather than publishing an event with no supported description.
    """
    summarised: list[AnalysedEvent] = []
    budget_spent = False

    for item in events[:limit]:
        analysis = None
        if not budget_spent:
            prompt = story_analysis_prompt(
                _documents_for(
                    item.event, articles, max_documents=SUMMARY_DOCUMENTS, max_chars=SUMMARY_CHARS
                )
            )
            try:
                analysis = provider.structured(prompt, StoryAnalysis)
            except BudgetExhaustedError:
                # Log once, not once per remaining story.
                logger.warning("call budget exhausted after %d summaries", len(summarised))
                budget_spent = True

        summarised.append(AnalysedEvent(scored=item.scored, impact=item.impact, analysis=analysis))

    return summarised + events[limit:]


def summarise(analysed: list[AnalysedEvent]) -> dict[str, int | float]:
    """Counts for the run log."""
    scored_by_model = sum(1 for item in analysed if item.model_scored)
    return {
        "events": len(analysed),
        "model_scored": scored_by_model,
        "degraded": len(analysed) - scored_by_model,
        "summarised": sum(1 for item in analysed if item.analysis is not None),
    }
