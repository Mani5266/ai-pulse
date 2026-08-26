"""Event clustering tests.

Several cases are false positives found by running the clusterer over one real day of 22
feeds. Each one merged articles that a reader would immediately see were different
stories, and each is now pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.models import Article, Event
from app.ingestion.normalize import enrich
from app.intelligence.categories import Category
from app.intelligence.clustering import ClusterConfig, cluster_articles

MONDAY = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
WEDNESDAY = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def make(
    title: str,
    *,
    source_id: str = "openai",
    url: str | None = None,
    summary: str | None = None,
    published_at: datetime = WEDNESDAY,
) -> Article:
    return enrich(
        Article(
            url=url or f"https://{source_id}.example.com/{abs(hash(title)) % 10**8}",  # type: ignore[arg-type]
            title=title,
            source_id=source_id,
            fetched_at=WEDNESDAY,
            published_at=published_at,
            summary=summary,
        )
    )


def test_one_event_per_article_when_nothing_is_related() -> None:
    result = cluster_articles(
        [
            make("OpenAI releases GPT-X", source_id="openai"),
            make("EU passes new AI liability rules", source_id="techcrunch-ai"),
            make("NVIDIA reports quarterly earnings", source_id="nvidia"),
        ]
    )

    assert len(result.events) == 3
    assert result.stats()["articles_per_event"] == 1.0


def test_two_outlets_covering_one_release_form_one_event() -> None:
    result = cluster_articles(
        [
            make("Introducing Gemma 4, a new open model", source_id="google-deepmind"),
            make("Google introduces Gemma 4 open model", source_id="the-verge-ai"),
        ]
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.article_count == 2
    assert event.source_count == 2
    assert event.canonical_title.startswith("Introducing Gemma 4")


def test_the_first_source_titles_the_event() -> None:
    """Registry order puts first-party announcements first, so they name the event."""
    result = cluster_articles(
        [
            make("Introducing Gemma 4, a new open model", source_id="google-deepmind"),
            make("Google introduces Gemma 4 open model", source_id="the-verge-ai"),
        ]
    )

    assert result.events[0].canonical_title == "Introducing Gemma 4, a new open model"
    assert result.events[0].source_ids == ["google-deepmind", "the-verge-ai"]


def test_a_shared_organisation_alone_does_not_merge() -> None:
    """Live false positive: twenty-two OpenAI articles sharing nothing but "OpenAI"."""
    result = cluster_articles(
        [
            make("OpenAI expands ads in Europe", source_id="openai"),
            make("OpenAI partners with a logistics firm", source_id="techcrunch-ai"),
        ]
    )

    assert len(result.events) == 2


def test_a_shared_product_alone_does_not_merge() -> None:
    """Live false positive: nine unrelated posts sharing only the word "ChatGPT"."""
    result = cluster_articles(
        [
            make("Premium seats are coming to ChatGPT Business", source_id="openai"),
            make("ChatGPT for Teens, built for learning", source_id="techcrunch-ai"),
        ]
    )

    assert len(result.events) == 2


def test_a_shared_model_family_alone_does_not_merge() -> None:
    """Gemini Robotics and Gemini Flash are both Gemini, and are two announcements."""
    result = cluster_articles(
        [
            make("Gemini Robotics 2 brings whole body intelligence", source_id="google-deepmind"),
            make("Get closer to the game with Gemini and Pixel", source_id="google-ai"),
        ]
    )

    assert len(result.events) == 2


def test_same_publisher_needs_near_duplicate_wording() -> None:
    """A publisher does not report its own news twice on the same day."""
    result = cluster_articles(
        [
            make("Introducing the Admin plugin for ChatGPT Work", source_id="openai"),
            make("Testing ads in ChatGPT", source_id="openai"),
            make("ChatGPT Ads expands across Europe", source_id="openai"),
        ]
    )

    assert len(result.events) == 3


def test_same_publisher_still_merges_a_near_identical_repost() -> None:
    result = cluster_articles(
        [
            make("Build a no-code ML workflow with SageMaker", source_id="aws-machine-learning"),
            make(
                "Build a no-code ML workflow with SageMaker Canvas",
                source_id="aws-machine-learning",
            ),
        ]
    )

    assert len(result.events) == 1


def test_conflicting_versions_never_merge() -> None:
    result = cluster_articles(
        [
            make("Introducing Gemini 3.5 Flash", source_id="google-deepmind"),
            make("Introducing Gemini 4 Flash", source_id="the-verge-ai"),
        ]
    )

    assert len(result.events) == 2


def test_a_write_up_omitting_the_version_still_joins() -> None:
    result = cluster_articles(
        [
            make("Introducing Gemini 3.5 Flash, our fastest model", source_id="google-deepmind"),
            make("Google is introducing Gemini Flash, its fastest model", source_id="the-verge-ai"),
        ]
    )

    assert len(result.events) == 1


def test_an_article_extends_an_event_from_an_earlier_day() -> None:
    """The timeline behaviour: Monday's announcement, Wednesday's availability."""
    monday = Event(
        id="evt_monday",
        canonical_title="Introducing Gemma 4, a new open model",
        category=Category.MODEL_RELEASE,
        entities=["model:gemma", "model:gemma-4", "org:google"],
        article_ids=["a1"],
        source_ids=["google-deepmind"],
        first_seen=MONDAY,
        last_updated=MONDAY,
    )

    result = cluster_articles(
        [make("Gemma 4 is now available on Hugging Face", source_id="huggingface-blog")],
        existing=[monday],
    )

    assert len(result.events) == 1
    updated = result.events[0]
    assert updated.id == "evt_monday"
    assert updated.first_seen == MONDAY
    assert updated.last_updated == WEDNESDAY
    assert updated.article_count == 2
    assert result.updated_event_ids == {"evt_monday"}
    assert result.new_event_ids == set()


def test_untouched_existing_events_are_not_rewritten() -> None:
    """Only events this run changed are returned, so unrelated history stays put."""
    stale = Event(
        id="evt_old",
        canonical_title="Something else entirely",
        category=Category.OTHER,
        entities=["org:meta"],
        article_ids=["a9"],
        source_ids=["meta-ai"],
        first_seen=MONDAY,
        last_updated=MONDAY,
    )

    result = cluster_articles([make("A wholly unrelated headline")], existing=[stale])

    assert [event.id for event in result.events] != ["evt_old"]
    assert all(event.id != "evt_old" for event in result.events)


def test_event_ids_are_stable_across_runs() -> None:
    articles = [make("Introducing Gemma 4, a new open model", source_id="google-deepmind")]

    first = cluster_articles(articles)
    second = cluster_articles(articles)

    assert first.events[0].id == second.events[0].id


def test_first_seen_uses_the_earliest_article() -> None:
    result = cluster_articles(
        [
            make("Introducing Gemma 4 open model", source_id="google-deepmind"),
            make(
                "Google introduces Gemma 4 open model",
                source_id="the-verge-ai",
                published_at=WEDNESDAY - timedelta(hours=3),
            ),
        ]
    )

    assert result.events[0].first_seen == WEDNESDAY - timedelta(hours=3)
    assert result.events[0].last_updated == WEDNESDAY


def test_threshold_is_configurable() -> None:
    articles = [
        make("Gemma 4 arrives", source_id="google-deepmind"),
        make("A different Gemma story", source_id="the-verge-ai"),
    ]

    strict = cluster_articles(articles, config=ClusterConfig(threshold=0.95))

    assert len(strict.events) == 2


def test_empty_input_produces_no_events() -> None:
    result = cluster_articles([])

    assert result.events == []
    assert result.stats()["articles"] == 0


def test_stats_report_corroboration() -> None:
    result = cluster_articles(
        [
            make("Introducing Gemma 4, a new open model", source_id="google-deepmind"),
            make("Google introduces Gemma 4 open model", source_id="the-verge-ai"),
            make("Unrelated policy news from Brussels", source_id="techcrunch-ai"),
        ]
    )

    stats = result.stats()

    assert stats["events_touched"] == 2
    assert stats["multi_source_events"] == 1
