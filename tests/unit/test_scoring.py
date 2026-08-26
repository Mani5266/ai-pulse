"""Deterministic scoring tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.models import Event
from app.intelligence.categories import Category
from app.ranking.profile import Profile
from app.ranking.scoring import (
    DETERMINISTIC_WEIGHT,
    Scores,
    score_credibility,
    score_events,
    score_novelty,
    score_personal_relevance,
)

TODAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
MONDAY = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)

CREDIBILITY = {"openai": 1.0, "techcrunch-ai": 0.85, "random-blog": 0.5}

PROFILE = Profile(
    interests=("agent", "open weights", "inference"),
    low_interest=("earnings", "hiring"),
    category_weights={
        Category.MODEL_RELEASE: 1.0,
        Category.RESEARCH: 0.55,
        Category.FUNDING: 0.25,
    },
)


def event(
    *,
    event_id: str = "evt_1",
    title: str = "Introducing a new model",
    category: Category = Category.MODEL_RELEASE,
    entities: list[str] | None = None,
    sources: list[str] | None = None,
    first_seen: datetime = NOW,
    last_updated: datetime = NOW,
) -> Event:
    return Event(
        id=event_id,
        canonical_title=title,
        category=category,
        entities=entities if entities is not None else ["model:gemma", "model:gemma-4"],
        article_ids=["a1"],
        source_ids=["openai"] if sources is None else sources,
        first_seen=first_seen,
        last_updated=last_updated,
    )


# --- credibility -------------------------------------------------------------


def test_a_better_source_scores_higher() -> None:
    best = score_credibility(event(sources=["openai"]), CREDIBILITY)
    worst = score_credibility(event(sources=["random-blog"]), CREDIBILITY)

    assert best > worst


def test_corroboration_raises_credibility() -> None:
    one = score_credibility(event(sources=["techcrunch-ai"]), CREDIBILITY)
    three = score_credibility(
        event(sources=["techcrunch-ai", "openai", "random-blog"]), CREDIBILITY
    )

    assert three > one


def test_corroboration_is_capped() -> None:
    """Without a cap, six aggregators would outrank a first-party announcement."""
    four = score_credibility(event(sources=[f"s{index}" for index in range(4)]), CREDIBILITY)
    eight = score_credibility(event(sources=[f"s{index}" for index in range(8)]), CREDIBILITY)

    assert four == eight


def test_an_unknown_source_gets_a_middling_score() -> None:
    unknown = score_credibility(event(sources=["never-heard-of-it"]), CREDIBILITY)

    assert 0.0 < unknown < score_credibility(event(sources=["openai"]), CREDIBILITY)


def test_an_event_with_no_sources_scores_zero() -> None:
    assert score_credibility(event(sources=[]), CREDIBILITY) == 0.0


# --- novelty -----------------------------------------------------------------


def test_a_new_event_beats_a_continuing_one() -> None:
    fresh = score_novelty(event(first_seen=NOW), today=TODAY, seen_entities=[])
    continuing = score_novelty(event(first_seen=MONDAY), today=TODAY, seen_entities=[])

    assert fresh > continuing


def test_familiar_entities_reduce_novelty() -> None:
    """A model that has been in the briefing all week is not news again."""
    unseen = score_novelty(event(), today=TODAY, seen_entities=[])
    seen = score_novelty(event(), today=TODAY, seen_entities=["model:gemma", "model:gemma-4"])

    assert unseen > seen


def test_partially_familiar_entities_score_in_between() -> None:
    entities = ["model:gemma", "model:gemma-4"]
    half = score_novelty(event(entities=entities), today=TODAY, seen_entities=["model:gemma"])
    none_seen = score_novelty(event(entities=entities), today=TODAY, seen_entities=[])
    all_seen = score_novelty(event(entities=entities), today=TODAY, seen_entities=entities)

    assert all_seen < half < none_seen


def test_an_event_with_no_specific_entities_is_neither_rewarded_nor_punished() -> None:
    score = score_novelty(event(entities=["org:google"]), today=TODAY, seen_entities=[])

    assert 0.0 < score < 10.0


# --- personal relevance ------------------------------------------------------


def test_category_weight_dominates_relevance() -> None:
    release = score_personal_relevance(event(category=Category.MODEL_RELEASE), PROFILE)
    funding = score_personal_relevance(event(category=Category.FUNDING), PROFILE)

    assert release > funding


def test_interest_terms_raise_relevance() -> None:
    plain = score_personal_relevance(event(title="A quiet announcement"), PROFILE)
    interesting = score_personal_relevance(
        event(title="A new coding agent with open weights"), PROFILE
    )

    assert interesting > plain


def test_low_interest_terms_lower_relevance() -> None:
    plain = score_personal_relevance(event(title="A quiet announcement"), PROFILE)
    dull = score_personal_relevance(event(title="Quarterly earnings and hiring update"), PROFILE)

    assert dull < plain


def test_interest_terms_match_whole_words() -> None:
    """Substring matching is the defect already found twice in this codebase."""
    profile = Profile(interests=("act",), category_weights={Category.OTHER: 0.5})
    accelerating = score_personal_relevance(
        event(title="Accelerating inference", category=Category.OTHER, entities=[]), profile
    )
    literal = score_personal_relevance(
        event(title="The act passed today", category=Category.OTHER, entities=[]), profile
    )

    assert literal > accelerating


def test_an_unlisted_category_is_neither_promoted_nor_buried() -> None:
    score = score_personal_relevance(event(category=Category.SECURITY), PROFILE)

    assert 0.0 < score < 10.0


# --- combination -------------------------------------------------------------


def test_the_weighted_score_is_rescaled_to_the_same_range() -> None:
    perfect = Scores(credibility=10.0, novelty=10.0, personal_relevance=10.0)
    nothing = Scores(credibility=0.0, novelty=0.0, personal_relevance=0.0)

    assert perfect.deterministic == 10.0
    assert nothing.deterministic == 0.0


def test_the_deterministic_weights_are_the_documented_share() -> None:
    """Three sub-scores at 0.15 each. The other 0.55 belongs to the model, in P5."""
    assert pytest.approx(0.45) == DETERMINISTIC_WEIGHT


def test_scoring_is_reproducible() -> None:
    events = [event(event_id="evt_a"), event(event_id="evt_b", title="Another release")]

    first = score_events(
        events, profile=PROFILE, source_credibility=CREDIBILITY, today=TODAY, seen_entities=[]
    )
    second = score_events(
        events, profile=PROFILE, source_credibility=CREDIBILITY, today=TODAY, seen_entities=[]
    )

    assert [item.event.id for item in first] == [item.event.id for item in second]
    assert [item.score for item in first] == [item.score for item in second]


def test_ties_are_broken_by_corroboration_then_recency_then_id() -> None:
    one_source = event(event_id="evt_b", sources=["openai"])
    two_sources = event(event_id="evt_a", sources=["openai", "techcrunch-ai"])

    ranked = score_events(
        [one_source, two_sources],
        profile=PROFILE,
        source_credibility=CREDIBILITY,
        today=TODAY,
        seen_entities=[],
    )

    assert ranked[0].event.id == "evt_a"


def test_scores_can_be_attached_to_the_event_for_storage() -> None:
    scored = score_events(
        [event()], profile=PROFILE, source_credibility=CREDIBILITY, today=TODAY, seen_entities=[]
    )[0]

    stored = scored.with_score()

    assert stored.importance_score == scored.score
    assert stored.id == scored.event.id


def test_an_empty_batch_scores_to_nothing() -> None:
    assert (
        score_events(
            [], profile=PROFILE, source_credibility=CREDIBILITY, today=TODAY, seen_entities=[]
        )
        == []
    )
