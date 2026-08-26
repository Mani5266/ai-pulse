"""Analysis stage tests: scoring, summarising, and degrading."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.models import Article, Event
from app.intelligence.categories import Category
from app.llm.analysis import (
    IMPACT_WEIGHT,
    AnalysedEvent,
    analyse_stories,
    score_impact,
    summarise,
)
from app.llm.provider import ScriptedProvider
from app.ranking.scoring import DETERMINISTIC_WEIGHT, ScoredEvent, Scores

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)

IMPACT = json.dumps(
    {
        "technical_impact": 8.0,
        "industry_impact": 6.0,
        "developer_impact": 7.0,
        "reasoning": "A capable new open model with permissive weights.",
    }
)

STORY = json.dumps(
    {
        "headline": "Gemma 4 released with open weights",
        "what_happened": "Google published Gemma 4 under a permissive licence.",
        "why_it_matters": "It is the strongest openly licensed model at this size.",
        "developer_impact": "Runs on a single consumer GPU.",
        "confidence": 0.85,
    }
)


def article(article_id: str, source: str = "google-deepmind") -> Article:
    return Article(
        url=f"https://{source}.example.com/{article_id}",  # type: ignore[arg-type]
        title=f"Article {article_id}",
        source_id=source,
        fetched_at=NOW,
        content="Some article body text.",
        id=article_id,
    )


def scored_event(
    event_id: str = "evt_1",
    *,
    article_ids: list[str] | None = None,
    score: float = 6.0,
) -> ScoredEvent:
    return ScoredEvent(
        event=Event(
            id=event_id,
            canonical_title="Gemma 4 arrives",
            category=Category.MODEL_RELEASE,
            entities=["model:gemma-4"],
            article_ids=article_ids or ["a1"],
            source_ids=["google-deepmind"],
            first_seen=NOW,
            last_updated=NOW,
        ),
        scores=Scores(credibility=score, novelty=score, personal_relevance=score),
    )


ARTICLES = {"a1": article("a1"), "a2": article("a2", "the-verge-ai")}


# --- impact scoring -----------------------------------------------------------


def test_impact_scores_are_attached() -> None:
    provider = ScriptedProvider([IMPACT])

    analysed = score_impact([scored_event()], ARTICLES, provider)

    assert analysed[0].impact is not None
    assert analysed[0].impact.technical_impact == 8.0
    assert analysed[0].model_scored is True


def test_one_call_is_made_per_event() -> None:
    provider = ScriptedProvider([IMPACT, IMPACT, IMPACT])

    score_impact(
        [scored_event("evt_1"), scored_event("evt_2"), scored_event("evt_3")],
        ARTICLES,
        provider,
    )

    assert provider.stats.attempted == 3


def test_the_final_score_combines_both_halves() -> None:
    provider = ScriptedProvider([IMPACT])

    analysed = score_impact([scored_event(score=5.0)], ARTICLES, provider)[0]

    expected = 5.0 * DETERMINISTIC_WEIGHT + 8.0 * 0.20 + 6.0 * 0.15 + 7.0 * 0.20
    assert analysed.final_score == round(expected, 3)


def test_the_weights_sum_to_one() -> None:
    """Half the formula is code, half is the model. Neither may drift."""
    assert round(DETERMINISTIC_WEIGHT + IMPACT_WEIGHT, 10) == 1.0


def test_a_failed_call_keeps_the_deterministic_score() -> None:
    """A missing signal must not read as a signal of zero."""
    provider = ScriptedProvider(["garbage", "still garbage"])

    analysed = score_impact([scored_event(score=6.0)], ARTICLES, provider)[0]

    assert analysed.impact is None
    assert analysed.model_scored is False
    assert analysed.final_score == 6.0


def test_an_exhausted_budget_stops_calls_without_losing_events() -> None:
    provider = ScriptedProvider([IMPACT] * 10, budget=2)

    analysed = score_impact(
        [scored_event(f"evt_{index}") for index in range(5)], ARTICLES, provider
    )

    assert len(analysed) == 5
    assert sum(1 for item in analysed if item.model_scored) == 2


def test_results_are_reordered_by_the_final_score() -> None:
    low = json.dumps(
        {
            "technical_impact": 1.0,
            "industry_impact": 1.0,
            "developer_impact": 1.0,
            "reasoning": "routine",
        }
    )
    provider = ScriptedProvider([low, IMPACT])

    analysed = score_impact(
        [scored_event("evt_low", score=6.0), scored_event("evt_high", score=6.0)],
        ARTICLES,
        provider,
    )

    assert [item.event.id for item in analysed] == ["evt_high", "evt_low"]


def test_documents_are_wrapped_before_reaching_the_model() -> None:
    provider = ScriptedProvider([IMPACT])

    score_impact([scored_event(article_ids=["a1", "a2"])], ARTICLES, provider)

    prompt = provider.prompts[0]
    assert "<document" in prompt
    assert "</document>" in prompt
    assert "Article a1" in prompt


def test_scoring_prompts_are_kept_small() -> None:
    """Groq's free tier limits tokens per minute, so scoring sends less than summarising.

    Measured: 4,000-character scoring prompts exhausted the per-minute allowance and 14 of
    25 calls failed.
    """
    many = {f"a{index}": article(f"a{index}") for index in range(10)}
    provider = ScriptedProvider([IMPACT, STORY])

    score_impact([scored_event(article_ids=list(many))], many, provider)
    scoring_prompt = provider.prompts[0]

    analyse_stories(
        [AnalysedEvent(scored=scored_event(article_ids=list(many)))], many, provider, limit=1
    )
    summary_prompt = provider.prompts[1]

    assert scoring_prompt.count("<document") == 3
    assert summary_prompt.count("<document") == 4
    assert len(scoring_prompt) < len(summary_prompt)


def test_a_missing_article_falls_back_to_the_event_title() -> None:
    """Retention prunes article text; the event must still be scoreable."""
    provider = ScriptedProvider([IMPACT])

    score_impact([scored_event(article_ids=["gone"])], {}, provider)

    assert "Gemma 4 arrives" in provider.prompts[0]


# --- story analysis -----------------------------------------------------------


def test_summaries_are_written_for_the_top_events_only() -> None:
    provider = ScriptedProvider([STORY, STORY])
    events = [AnalysedEvent(scored=scored_event(f"evt_{index}")) for index in range(5)]

    summarised = analyse_stories(events, ARTICLES, provider, limit=2)

    assert sum(1 for item in summarised if item.analysis is not None) == 2
    assert len(summarised) == 5


def test_a_failed_summary_leaves_the_event_without_one() -> None:
    provider = ScriptedProvider(["nope", "nope"])
    events = [AnalysedEvent(scored=scored_event())]

    summarised = analyse_stories(events, ARTICLES, provider, limit=1)

    assert summarised[0].analysis is None


def test_the_summary_is_persisted_with_the_event() -> None:
    provider = ScriptedProvider([STORY])
    events = [AnalysedEvent(scored=scored_event())]

    summarised = analyse_stories(events, ARTICLES, provider, limit=1)
    stored = summarised[0].with_score()

    assert stored.description is not None
    assert stored.confidence == 0.85
    assert stored.importance_score == summarised[0].final_score


def test_impact_scores_survive_the_summary_stage() -> None:
    provider = ScriptedProvider([IMPACT, STORY])

    analysed = score_impact([scored_event()], ARTICLES, provider)
    summarised = analyse_stories(analysed, ARTICLES, provider, limit=1)

    assert summarised[0].impact is not None
    assert summarised[0].analysis is not None


# --- reporting ----------------------------------------------------------------


def test_the_run_summary_counts_degradation() -> None:
    provider = ScriptedProvider([IMPACT, "bad", "bad"])

    analysed = score_impact([scored_event("evt_1"), scored_event("evt_2")], ARTICLES, provider)
    stats = summarise(analysed)

    assert stats["events"] == 2
    assert stats["model_scored"] == 1
    assert stats["degraded"] == 1


def test_a_nominal_run_fits_the_budget() -> None:
    """20 shortlisted events plus 5 stories is 25 calls, inside the 40-call ceiling."""
    provider = ScriptedProvider([IMPACT] * 20 + [STORY] * 5, budget=40)

    analysed = score_impact(
        [scored_event(f"evt_{index}") for index in range(20)], ARTICLES, provider
    )
    analyse_stories(analysed, ARTICLES, provider, limit=5)

    assert provider.stats.attempted == 25
    assert provider.remaining == 15


def test_scoring_reserves_budget_for_the_summaries() -> None:
    """Live failure: twenty events retrying once each consumed the whole forty-call
    budget, and every summary was skipped. Scoring degrades gracefully; a briefing with
    no prose does not."""
    # Scoring stops once only the 10 reserved calls remain, so it burns exactly 30.
    provider = ScriptedProvider(["bad"] * 30 + [STORY] * 5, budget=40)

    analysed = score_impact(
        [scored_event(f"evt_{index}") for index in range(20)],
        ARTICLES,
        provider,
        reserve=10,
    )
    summarised = analyse_stories(analysed, ARTICLES, provider, limit=5)

    assert provider.remaining >= 0
    assert sum(1 for item in summarised if item.analysis is not None) == 5


def test_without_a_reservation_scoring_may_spend_everything() -> None:
    """The behaviour the reservation exists to prevent, pinned so it cannot return."""
    provider = ScriptedProvider(["bad"] * 40, budget=40)

    score_impact([scored_event(f"evt_{index}") for index in range(20)], ARTICLES, provider)

    assert provider.remaining == 0
