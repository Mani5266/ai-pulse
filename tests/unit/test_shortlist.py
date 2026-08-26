"""Shortlist tests: the cut that keeps the run inside a free API tier."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.models import Event
from app.intelligence.categories import Category
from app.ranking.scoring import ScoredEvent, Scores
from app.ranking.shortlist import build_shortlist

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def scored(
    event_id: str,
    score: float,
    category: Category = Category.RESEARCH,
    *,
    sources: int = 1,
) -> ScoredEvent:
    """A ScoredEvent whose deterministic score is exactly ``score``.

    All three sub-scores are equal, so the weighted rescale returns the same number.
    """
    return ScoredEvent(
        event=Event(
            id=event_id,
            canonical_title=f"Story {event_id}",
            category=category,
            entities=[],
            article_ids=["a1"],
            source_ids=[f"source-{index}" for index in range(sources)],
            first_seen=NOW,
            last_updated=NOW,
        ),
        scores=Scores(credibility=score, novelty=score, personal_relevance=score),
    )


def test_the_limit_is_respected() -> None:
    events = [scored(f"evt_{index}", 9.0 - index * 0.1) for index in range(50)]

    result = build_shortlist(events, limit=20)

    assert len(result.selected) == 20


def test_the_best_events_are_selected() -> None:
    events = [
        scored("evt_high", 9.0, Category.MODEL_RELEASE),
        scored("evt_mid", 5.0, Category.POLICY),
        scored("evt_low", 1.0, Category.OTHER),
    ]

    result = build_shortlist(events, limit=2)

    assert [item.event.id for item in result.selected] == ["evt_high", "evt_mid"]


def test_one_category_cannot_fill_the_shortlist() -> None:
    """Eighty papers a day would otherwise crowd out every other kind of development.

    The papers all outscore the alternatives, so a plain top-N would take six papers.
    """
    papers = [
        scored(f"paper_{index}", 9.0 - index * 0.01, Category.RESEARCH) for index in range(20)
    ]
    others = [
        scored("evt_release", 5.0, Category.MODEL_RELEASE),
        scored("evt_policy", 4.0, Category.POLICY),
        scored("evt_security", 3.0, Category.SECURITY),
    ]

    result = build_shortlist([*papers, *others], limit=6, max_per_category=3)

    categories = result.categories()
    assert categories["research"] == 3
    assert sum(categories.values()) == 6
    assert result.stats()["dropped_by_category_cap"] > 0


def test_corroborated_events_bypass_the_category_cap() -> None:
    """Live failure: a Gemma 4 release covered by three sources was dropped in favour of
    four single-source posts that happened to share its category."""
    fillers = [
        scored(f"evt_{index}", 9.0 - index * 0.1, Category.MODEL_RELEASE) for index in range(4)
    ]
    corroborated = scored("evt_gemma", 7.0, Category.MODEL_RELEASE, sources=3)

    result = build_shortlist([*fillers, corroborated], limit=10, max_per_category=4)

    assert "evt_gemma" in {item.event.id for item in result.selected}
    assert result.stats()["corroborated"] == 1


def test_spare_slots_are_filled_even_if_the_caps_are_reached() -> None:
    """A quiet day dominated by one category should still fill the shortlist."""
    papers = [scored(f"paper_{index}", 9.0 - index * 0.1, Category.RESEARCH) for index in range(10)]

    result = build_shortlist(papers, limit=8, max_per_category=3)

    assert len(result.selected) == 8


def test_the_shortlist_comes_back_in_score_order() -> None:
    """The briefing leads with the biggest story, so the category walk must not reorder."""
    events = [
        scored("evt_a", 9.0, Category.RESEARCH),
        scored("evt_b", 8.0, Category.RESEARCH),
        scored("evt_c", 7.0, Category.POLICY),
        scored("evt_d", 6.0, Category.RESEARCH),
    ]

    result = build_shortlist(events, limit=4, max_per_category=2)

    assert [item.score for item in result.selected] == sorted(
        (item.score for item in result.selected), reverse=True
    )


def test_stats_describe_the_cut() -> None:
    events = [scored(f"evt_{index}", 9.0 - index * 0.1) for index in range(30)]

    stats = build_shortlist(events, limit=5, max_per_category=5).stats()

    assert stats["considered"] == 30
    assert stats["selected"] == 5
    assert stats["top_score"] >= stats["cut_off_score"]


def test_an_empty_input_produces_an_empty_shortlist() -> None:
    result = build_shortlist([], limit=20)

    assert result.selected == []
    assert result.stats()["top_score"] == 0.0


def test_fewer_events_than_the_limit_are_all_selected() -> None:
    events = [scored("evt_a", 9.0), scored("evt_b", 8.0)]

    result = build_shortlist(events, limit=20)

    assert len(result.selected) == 2
