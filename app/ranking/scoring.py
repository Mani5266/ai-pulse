"""Deterministic relevance scoring.

Half of the importance formula is computed here, in ordinary Python, before any model is
involved:

===================== ======== ==========================================================
Sub-score             Weight   Source
===================== ======== ==========================================================
``credibility``       0.15     Source registry, plus corroboration across sources
``novelty``           0.15     Event history — is this new, or the same story again?
``personal_relevance``0.15     The profile in ``config/profile.yaml``
--------------------- -------- ----------------------------------------------------------
``technical_impact``  0.20     LLM, in P5
``industry_impact``   0.15     LLM, in P5
``developer_impact``  0.20     LLM, in P5
===================== ======== ==========================================================

The original design had the model produce five of the six and called the result
deterministic. A weighted average of model guesses is not deterministic. Splitting them
this way makes half the formula reproducible, and — more importantly — lets the
deterministic half cut roughly 500 events down to 20 *before* the first model call, which
is what keeps the run inside a free API tier.

Every sub-score is on a 0 to 10 scale. The P4 score is the deterministic half rescaled to
0 to 10 so that it is readable on its own; P5 recomputes the full weighted score once the
impact scores exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from app.core.models import Event
from app.intelligence.entities import DECISIVE_WEIGHT, weight
from app.ranking.profile import Profile

CREDIBILITY_WEIGHT = 0.15
NOVELTY_WEIGHT = 0.15
PERSONAL_WEIGHT = 0.15
DETERMINISTIC_WEIGHT = CREDIBILITY_WEIGHT + NOVELTY_WEIGHT + PERSONAL_WEIGHT

MAX_SCORE = 10.0


@dataclass(frozen=True, slots=True)
class Scores:
    """The deterministic half of the importance formula, for one event."""

    credibility: float
    novelty: float
    personal_relevance: float

    @property
    def deterministic(self) -> float:
        """The three sub-scores, weighted and rescaled to 0 to 10.

        Rescaling matters: without it every event would score at most 4.5 and the number
        would be unreadable next to the sub-scores it came from.
        """
        weighted = (
            self.credibility * CREDIBILITY_WEIGHT
            + self.novelty * NOVELTY_WEIGHT
            + self.personal_relevance * PERSONAL_WEIGHT
        )
        return round(weighted / DETERMINISTIC_WEIGHT, 3)

    def as_dict(self) -> dict[str, float]:
        return {
            "credibility": self.credibility,
            "novelty": self.novelty,
            "personal_relevance": self.personal_relevance,
            "deterministic": self.deterministic,
        }


@dataclass(frozen=True, slots=True)
class ScoredEvent:
    """An event with its deterministic scores attached."""

    event: Event
    scores: Scores

    @property
    def score(self) -> float:
        return self.scores.deterministic

    def with_score(self) -> Event:
        """The event, carrying its score, ready to persist."""
        return self.event.model_copy(update={"importance_score": self.score})


def _clamp(value: float) -> float:
    return round(max(0.0, min(MAX_SCORE, value)), 3)


def score_credibility(event: Event, source_credibility: dict[str, float]) -> float:
    """How much the sources justify believing this happened.

    The best source sets the floor, and each additional independent source raises it.
    Corroboration is capped: the fourth outlet to cover a release adds nothing the third
    did not, and without a cap a story covered by six aggregators would outrank a
    first-party announcement.
    """
    if not event.source_ids:
        return 0.0

    best = max(source_credibility.get(source_id, 0.5) for source_id in event.source_ids)
    corroboration = min(len(event.source_ids) - 1, 3)
    return _clamp(best * 7.0 + corroboration)


def score_novelty(event: Event, *, today: date, seen_entities: Iterable[str]) -> float:
    """How much of this is actually new.

    Two independent questions. Is the *event* new, or a story already reported that has
    merely been updated? And are its *entities* new, or has this model, product or company
    been in the briefing all week? A fifth article about a release announced on Monday is
    a development, not news, and should not outrank Wednesday's first announcement.
    """
    known = set(seen_entities)

    is_new_event = event.first_seen.date() >= today
    base = 6.0 if is_new_event else 3.0

    specific = [entity for entity in event.entities if weight(entity) >= DECISIVE_WEIGHT - 0.4]
    if specific:
        unseen = sum(1 for entity in specific if entity not in known)
        base += 4.0 * (unseen / len(specific))
    else:
        # Nothing specific to be novel about. Neither rewarded nor punished.
        base += 1.0

    return _clamp(base)


def score_personal_relevance(event: Event, profile: Profile) -> float:
    """How much this particular reader cares.

    Category weight carries most of it, because category is the most reliable signal
    available without a model: eighty papers a day are interesting in principle and cannot
    all be in a sixty-second briefing. Explicit interests then adjust within that.
    """
    haystack = f"{event.canonical_title} {' '.join(event.entities)}"
    interest_hits, low_hits = profile.matches(haystack)

    category_part = profile.weight_for(event.category) * 6.0
    interest_part = min(interest_hits, 2) * 2.0
    penalty = min(low_hits, 2) * 2.0

    return _clamp(category_part + interest_part - penalty)


def score_event(
    event: Event,
    *,
    profile: Profile,
    source_credibility: dict[str, float],
    today: date,
    seen_entities: Iterable[str],
) -> ScoredEvent:
    """Score one event on the deterministic half of the formula."""
    scores = Scores(
        credibility=score_credibility(event, source_credibility),
        novelty=score_novelty(event, today=today, seen_entities=seen_entities),
        personal_relevance=score_personal_relevance(event, profile),
    )
    return ScoredEvent(event=event, scores=scores)


def score_events(
    events: Sequence[Event],
    *,
    profile: Profile,
    source_credibility: dict[str, float],
    today: date,
    seen_entities: Iterable[str],
) -> list[ScoredEvent]:
    """Score a batch, highest first.

    Ties are broken deterministically — more sources, then more recently updated, then by
    id — so that two runs over the same data produce the same order. A ranking that
    reshuffles on every run cannot be evaluated in P9.
    """
    known = set(seen_entities)
    scored = [
        score_event(
            event,
            profile=profile,
            source_credibility=source_credibility,
            today=today,
            seen_entities=known,
        )
        for event in events
    ]
    scored.sort(
        key=lambda item: (
            -item.score,
            -item.event.source_count,
            -item.event.last_updated.timestamp(),
            item.event.id,
        )
    )
    return scored
