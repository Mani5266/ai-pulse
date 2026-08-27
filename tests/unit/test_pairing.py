"""Duplicate adjudication: which pairs are worth a model call, and what a merge keeps.

The case that forced this module is in `test_two_outlets_wording_one_event_are_candidates`:
one OpenAI incident took two of five briefing slots on 27 August because two outlets
described it in different words and neither named a shared version. Everything else here
guards the opposite risk — a wrong merge deletes a story silently, so most of these tests
assert that a pair is *not* proposed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.models import Event
from app.intelligence.categories import Category
from app.intelligence.pairing import SIMILARITY_FLOOR, candidate_pairs, merge_events

NOW = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)


def event(
    event_id: str,
    title: str,
    *,
    entities: list[str] | None = None,
    sources: list[str] | None = None,
    articles: list[str] | None = None,
    category: Category = Category.AI_AGENTS,
    first_seen: datetime = NOW,
    last_updated: datetime = NOW,
) -> Event:
    return Event(
        id=event_id,
        canonical_title=title,
        category=category,
        entities=entities or [],
        article_ids=articles or [f"art_{event_id}"],
        source_ids=sources or [f"src_{event_id}"],
        first_seen=first_seen,
        last_updated=last_updated,
    )


# --- what should be asked about ------------------------------------------------


def test_two_outlets_wording_one_event_are_candidates() -> None:
    """The 27 August duplicate, reconstructed from the two headlines that shipped."""
    left = event(
        "evt_a",
        "OpenAI's unreleased model escaped containment, accessed internet and hacked Hugging Face",
        entities=["openai", "hugging face"],
    )
    right = event(
        "evt_b",
        "OpenAI agents hacked Hugging Face after being inadvertently trained to cheat",
        entities=["openai", "hugging face"],
    )

    pairs = candidate_pairs([left, right])

    assert len(pairs) == 1
    assert pairs[0].key == ("evt_a", "evt_b")
    assert pairs[0].similarity >= SIMILARITY_FLOOR


def test_the_strongest_candidates_come_first() -> None:
    """Budget is five pairs, so the ordering decides which questions get asked at all."""
    base = event("evt_a", "OpenAI agents breached a model hub", entities=["openai"])
    close = event("evt_b", "OpenAI agents breached a hub", entities=["openai"])
    distant = event("evt_c", "OpenAI publishes a report", entities=["openai"])

    pairs = candidate_pairs([base, close, distant])

    assert pairs[0].key == ("evt_a", "evt_b")
    assert pairs[0].similarity >= pairs[-1].similarity


def test_only_the_best_few_are_returned() -> None:
    """Names carry the variation: a number in a title would trip the identity guard."""
    names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    events = [event(f"evt_{n}", f"OpenAI agents breached a model hub in {n}") for n in names]

    assert len(candidate_pairs(events, limit=3)) == 3


# --- what must never be asked about --------------------------------------------


def test_unrelated_events_are_not_candidates() -> None:
    left = event("evt_a", "Qwen releases a multimodal model", entities=["qwen"])
    right = event("evt_b", "Regulators publish a data protection ruling", entities=["eu"])

    assert candidate_pairs([left, right]) == []


def test_different_versions_never_pair() -> None:
    """Two releases from one lab. Clustering's rule, applied so a merge cannot undo it."""
    left = event("evt_a", "Gemma 4 released with longer context", entities=["gemma 4", "google"])
    right = event("evt_b", "Gemma 3 released with longer context", entities=["gemma 3", "google"])

    assert candidate_pairs([left, right]) == []


def test_titles_differing_only_by_a_month_never_pair() -> None:
    """Two monthly roundups. Trigram similarity is nearly blind to the one word that matters."""
    left = event("evt_a", "The AI news we announced in July 2026")
    right = event("evt_b", "The AI news we announced in June 2026")

    assert candidate_pairs([left, right]) == []


def test_different_categories_never_pair() -> None:
    """A release and a funding round are not one development, even naming one company."""
    left = event("evt_a", "OpenAI ships a new agent", category=Category.MODEL_RELEASE)
    right = event("evt_b", "OpenAI ships a new agent", category=Category.FUNDING)

    assert candidate_pairs([left, right]) == []


def test_a_single_event_produces_no_pairs() -> None:
    assert candidate_pairs([event("evt_a", "Something happened")]) == []


def test_one_generic_shared_entity_is_not_evidence() -> None:
    """Three unrelated live stories paired at 0.5 because each carried `term:url` and

    nothing else. `weighted_overlap` reports total agreement when two thin entity sets
    match, which would have spent three of five model calls on nonsense and invited a
    merge that deletes a story. Note the junk entity weighs 0.6 and the real duplicate's
    entities weigh 0.25, so no weight threshold separates these — only the count does.
    """
    left = event("evt_a", "Tailcat is like netcat over a mesh network", entities=["term:url"])
    right = event("evt_b", "State Department pauses visa applications", entities=["term:url"])

    assert candidate_pairs([left, right]) == []


def test_two_shared_entities_corroborate_each_other() -> None:
    """The real 27 August duplicate: two organisations shared, wording of only 0.195."""
    left = event(
        "evt_a",
        "OpenAI's rogue AI model incident was worse than we thought",
        entities=["org:openai", "org:huggingface"],
    )
    right = event(
        "evt_b",
        "The inside story on why OpenAI agents hacked Hugging Face",
        entities=["org:openai", "org:huggingface"],
    )

    assert len(candidate_pairs([left, right])) == 1


def test_a_decisive_shared_entity_is_enough_on_its_own() -> None:
    """A model family plus version. The only entity strong enough to carry a pair alone."""
    left = event("evt_a", "Containment failure at a frontier lab", entities=["model:gemma-4"])
    right = event(
        "evt_b", "Weights published under a permissive licence", entities=["model:gemma-4"]
    )

    assert len(candidate_pairs([left, right])) == 1


def test_strong_wording_stands_in_for_entity_evidence() -> None:
    """Two near-identical headlines naming nothing known are still one story."""
    left = event("evt_a", "Regulators open an inquiry into autonomous coding agents")
    right = event("evt_b", "Regulators open an inquiry into autonomous agents")

    assert len(candidate_pairs([left, right])) == 1


# --- what a merge keeps --------------------------------------------------------


def test_a_merge_keeps_the_preferred_record_and_unions_the_rest() -> None:
    keep = event(
        "evt_a",
        "OpenAI's unreleased model escaped containment",
        entities=["openai"],
        sources=["theverge"],
        articles=["art_1"],
        first_seen=NOW,
        last_updated=NOW,
    )
    drop = event(
        "evt_b",
        "OpenAI agents hacked Hugging Face",
        entities=["hugging face"],
        sources=["techcrunch"],
        articles=["art_2"],
        first_seen=NOW - timedelta(hours=5),
        last_updated=NOW + timedelta(hours=2),
    )

    merged = merge_events(keep, drop)

    assert merged.id == "evt_a"
    assert merged.canonical_title == keep.canonical_title
    assert merged.source_ids == ["theverge", "techcrunch"]
    assert merged.article_ids == ["art_1", "art_2"]
    assert merged.entities == ["openai", "hugging face"]
    # The dates widen both ways, so a timeline still spans the whole development.
    assert merged.first_seen == NOW - timedelta(hours=5)
    assert merged.last_updated == NOW + timedelta(hours=2)


def test_a_merge_raises_the_source_count_that_signals_corroboration() -> None:
    """This is what the merge buys downstream: two outlets, not two lonely stories."""
    keep = event("evt_a", "A headline", sources=["theverge"])
    drop = event("evt_b", "Another headline", sources=["techcrunch"])

    assert keep.source_count == 1
    assert merge_events(keep, drop).source_count == 2


def test_a_merge_does_not_double_count_a_shared_source() -> None:
    keep = event("evt_a", "A headline", sources=["theverge"], articles=["art_1"])
    drop = event("evt_b", "Another headline", sources=["theverge"], articles=["art_1", "art_2"])

    merged = merge_events(keep, drop)

    assert merged.source_ids == ["theverge"]
    assert merged.article_ids == ["art_1", "art_2"]
