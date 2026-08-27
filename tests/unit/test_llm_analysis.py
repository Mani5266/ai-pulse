"""Analysis stage tests: scoring, summarising, and degrading."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.models import Article, Event
from app.intelligence.categories import Category
from app.intelligence.verification import VerificationStatus
from app.llm.analysis import (
    IMPACT_WEIGHT,
    AnalysedEvent,
    analyse_stories,
    merge_duplicates,
    score_impact,
    summarise,
    verify_claims,
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


CLAIMS = json.dumps(
    {
        "claims": [
            {
                "text": "Gemma 4 has 12 billion parameters",
                "supported_by": ["google-deepmind", "the-verge-ai"],
                "contradicted_by": [],
            },
            {
                "text": "It scores 71 on MMLU",
                "supported_by": ["google-deepmind"],
                "contradicted_by": [],
            },
        ]
    }
)


def multi_source_event(event_id: str = "evt_multi") -> ScoredEvent:
    base = scored_event(event_id)
    return ScoredEvent(
        event=base.event.model_copy(
            update={"source_ids": ["google-deepmind", "the-verge-ai"], "article_ids": ["a1", "a2"]}
        ),
        scores=base.scores,
    )


def test_claims_are_extracted_and_labelled() -> None:
    provider = ScriptedProvider([CLAIMS])
    events = [AnalysedEvent(scored=multi_source_event())]

    verified = verify_claims(events, ARTICLES, provider, limit=1)

    assert len(verified[0].claims) == 2
    assert verified[0].claims[0].status is VerificationStatus.VERIFIED
    assert verified[0].claims[1].status is VerificationStatus.UNVERIFIED


def test_a_single_source_event_costs_no_model_call() -> None:
    """A single source cannot corroborate itself, so the source count already gives the
    answer. On a typical day that is most of the shortlist."""
    provider = ScriptedProvider([CLAIMS])
    events = [AnalysedEvent(scored=scored_event())]

    verified = verify_claims(events, ARTICLES, provider, limit=1)

    assert provider.stats.attempted == 0
    assert verified[0].claims == ()


def test_a_failed_extraction_leaves_the_story_without_claims() -> None:
    provider = ScriptedProvider(["nonsense", "still nonsense"])
    events = [AnalysedEvent(scored=multi_source_event())]

    verified = verify_claims(events, ARTICLES, provider, limit=1)

    assert verified[0].claims == ()
    assert verified[0].scored is events[0].scored


def test_verification_preserves_the_earlier_analysis() -> None:
    provider = ScriptedProvider([IMPACT, STORY, CLAIMS])

    analysed = score_impact([multi_source_event()], ARTICLES, provider)
    analysed = analyse_stories(analysed, ARTICLES, provider, limit=1)
    verified = verify_claims(analysed, ARTICLES, provider, limit=1)

    assert verified[0].impact is not None
    assert verified[0].analysis is not None
    assert len(verified[0].claims) == 2


def test_only_the_briefing_stories_are_verified() -> None:
    provider = ScriptedProvider([CLAIMS])
    events = [AnalysedEvent(scored=multi_source_event(f"evt_{i}")) for i in range(4)]

    verified = verify_claims(events, ARTICLES, provider, limit=1)

    assert provider.stats.attempted == 1
    assert len(verified) == 4


def test_the_run_summary_counts_claims() -> None:
    provider = ScriptedProvider([CLAIMS])
    events = [AnalysedEvent(scored=multi_source_event())]

    stats = summarise(verify_claims(events, ARTICLES, provider, limit=1))

    assert stats["claims"] == 2
    assert stats["corroborated_claims"] == 1


# --- duplicate adjudication ---------------------------------------------------

SAME = json.dumps({"same_event": True, "confidence": 0.9, "reasoning": "One incident."})
DIFFERENT = json.dumps({"same_event": False, "confidence": 0.9, "reasoning": "Two releases."})
UNSURE = json.dumps({"same_event": True, "confidence": 0.4, "reasoning": "Possibly the same."})


def agent_event(event_id: str, title: str, *, source: str, score: float = 6.0) -> ScoredEvent:
    """Two of these differ in wording but describe one development."""
    return ScoredEvent(
        event=Event(
            id=event_id,
            canonical_title=title,
            category=Category.AI_AGENTS,
            entities=["openai", "hugging face"],
            article_ids=[f"art_{event_id}"],
            source_ids=[source],
            first_seen=NOW,
            last_updated=NOW,
        ),
        scores=Scores(credibility=score, novelty=score, personal_relevance=score),
    )


def duplicate_pair() -> list[ScoredEvent]:
    return [
        agent_event(
            "evt_a",
            "OpenAI's unreleased model escaped containment and hacked Hugging Face",
            source="theverge",
            score=7.0,
        ),
        agent_event(
            "evt_b",
            "OpenAI agents hacked Hugging Face after being trained to cheat",
            source="techcrunch",
            score=5.0,
        ),
    ]


def test_a_confirmed_duplicate_is_folded_into_the_higher_scored_event() -> None:
    """The 27 August defect: one incident took two of five briefing slots."""
    provider = ScriptedProvider([SAME])

    selected, merges = merge_duplicates(duplicate_pair(), provider)

    assert merges == 1
    assert len(selected) == 1
    assert selected[0].event.id == "evt_a"
    assert selected[0].event.source_ids == ["theverge", "techcrunch"]


def test_a_merge_leaves_the_briefing_a_free_slot() -> None:
    """The point of the exercise: a slot returned to a story that is not a repeat."""
    other = agent_event("evt_c", "Regulators open an inquiry into agents", source="ft")
    provider = ScriptedProvider([SAME])

    selected, _ = merge_duplicates([*duplicate_pair(), other], provider)

    assert [item.event.id for item in selected] == ["evt_a", "evt_c"]


def test_a_rejected_pair_leaves_both_events_standing() -> None:
    provider = ScriptedProvider([DIFFERENT])

    selected, merges = merge_duplicates(duplicate_pair(), provider)

    assert merges == 0
    assert len(selected) == 2


def test_an_unconfident_yes_is_not_a_merge() -> None:
    """A wrong merge deletes a story silently, so uncertainty resolves to leaving them apart."""
    provider = ScriptedProvider([UNSURE])

    selected, merges = merge_duplicates(duplicate_pair(), provider)

    assert merges == 0
    assert len(selected) == 2


def test_nothing_is_asked_when_no_pair_is_plausible() -> None:
    """No candidates means no calls: adjudication is free on a day with no duplicates."""
    left = agent_event("evt_a", "Qwen ships a multimodal model", source="qwen")
    right = agent_event("evt_b", "Regulators publish a ruling", source="ft")
    right = ScoredEvent(
        event=right.event.model_copy(update={"entities": ["eu"]}), scores=right.scores
    )
    provider = ScriptedProvider([SAME])

    selected, merges = merge_duplicates([left, right], provider)

    assert merges == 0
    assert len(selected) == 2
    assert provider.stats.attempted == 0


def test_a_single_event_is_never_adjudicated() -> None:
    provider = ScriptedProvider([SAME])

    selected, merges = merge_duplicates([agent_event("evt_a", "One story", source="x")], provider)

    assert (len(selected), merges) == (1, 0)
    assert provider.stats.attempted == 0


def test_adjudication_respects_the_reserve() -> None:
    """Merging must never be the reason a briefing has no prose."""
    provider = ScriptedProvider([SAME], budget=4)

    selected, merges = merge_duplicates(duplicate_pair(), provider, reserve=4)

    assert merges == 0
    assert len(selected) == 2
    assert provider.stats.attempted == 0


def test_a_failed_adjudication_is_not_fatal() -> None:
    """A provider with nothing scripted raises inside; both events must survive it."""
    provider = ScriptedProvider([])

    selected, merges = merge_duplicates(duplicate_pair(), provider)

    assert merges == 0
    assert len(selected) == 2


def test_adjudication_is_disabled_by_a_zero_limit() -> None:
    provider = ScriptedProvider([SAME])

    selected, merges = merge_duplicates(duplicate_pair(), provider, limit=0)

    assert (len(selected), merges) == (2, 0)
    assert provider.stats.attempted == 0
