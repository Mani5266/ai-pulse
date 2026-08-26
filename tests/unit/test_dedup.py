"""Deduplication tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.models import Article
from app.ingestion.dedup import deduplicate
from app.ingestion.normalize import enrich, enrich_all

FETCHED_AT = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)


def make(
    url: str,
    title: str = "OpenAI releases GPT-X",
    *,
    source_id: str = "openai",
    summary: str | None = None,
) -> Article:
    return enrich(
        Article(
            url=url,  # type: ignore[arg-type]
            title=title,
            source_id=source_id,
            fetched_at=FETCHED_AT,
            summary=summary,
        )
    )


def test_url_variants_of_one_article_collapse() -> None:
    articles = [
        make("https://example.com/a"),
        make("https://example.com/a/"),
        make("https://www.example.com/a?utm_source=news"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 1
    assert result.counts_by_reason() == {"duplicate_url": 2}


def test_syndicated_copy_under_a_different_url_is_caught_by_content_hash() -> None:
    articles = [
        make("https://origin.com/story", "Model X ships", summary="Details here."),
        make("https://syndicate.com/story-copy", "Model X ships", summary="Details here."),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 1
    assert result.counts_by_reason() == {"duplicate_content": 1}
    assert str(result.unique[0].url) == "https://origin.com/story"


def test_lightly_reworded_headline_is_caught_by_title_similarity() -> None:
    articles = [
        make("https://a.com/1", "OpenAI releases GPT-X today", summary="one"),
        make("https://b.com/2", "OpenAI releases GPT-X today.", summary="two"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 1
    assert result.counts_by_reason() == {"similar_title": 1}


def test_different_stories_survive() -> None:
    articles = [
        make("https://a.com/1", "OpenAI releases GPT-X"),
        make("https://a.com/2", "EU passes new AI liability rules"),
        make("https://a.com/3", "NVIDIA reports quarterly earnings"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 3
    assert result.duplicates == []


def test_first_copy_wins_so_registry_order_decides() -> None:
    """Sources are ingested primary first, so the first-party post outlives the write-up."""
    articles = [
        make("https://openai.com/post", "GPT-X is here", source_id="openai", summary="s"),
        make("https://techcrunch.com/post", "GPT-X is here", source_id="techcrunch", summary="s"),
    ]

    result = deduplicate(articles)

    assert [article.source_id for article in result.unique] == ["openai"]
    assert result.duplicates[0].source_id == "techcrunch"
    assert result.duplicates[0].kept_id == result.unique[0].id


def test_known_ids_suppress_articles_seen_on_earlier_days() -> None:
    article = make("https://example.com/a")

    result = deduplicate([article], known_ids={article.id or ""})

    assert result.unique == []
    assert result.counts_by_reason() == {"duplicate_url": 1}


def test_known_content_hashes_suppress_syndication_across_days() -> None:
    yesterday = make("https://origin.com/x", "Model X ships", summary="Details.")
    today = make("https://other.com/y", "Model X ships", summary="Details.")

    result = deduplicate([today], known_content_hashes={yesterday.content_hash or ""})

    assert result.unique == []
    assert result.counts_by_reason() == {"duplicate_content": 1}


def test_threshold_is_respected() -> None:
    articles = [
        make("https://a.com/1", "Model X released by OpenAI", summary="one"),
        make("https://a.com/2", "Model X launched by OpenAI", summary="two"),
    ]

    strict = deduplicate(articles, title_threshold=0.99)
    loose = deduplicate(articles, title_threshold=0.7)

    assert len(strict.unique) == 2
    assert len(loose.unique) == 1


def test_stats_report_the_duplicate_rate() -> None:
    articles = [make("https://example.com/a"), make("https://example.com/a/")]

    stats = deduplicate(articles).stats()

    assert stats["input"] == 2
    assert stats["unique"] == 1
    assert stats["duplicates"] == 1
    assert stats["duplicate_rate"] == 0.5


def test_empty_input_is_handled() -> None:
    result = deduplicate([])

    assert result.unique == []
    assert result.duplicate_rate == 0.0
    assert result.stats()["input"] == 0


def test_articles_without_titles_are_not_merged_by_similarity() -> None:
    """An empty trigram set must not match everything."""
    articles = enrich_all(
        [
            Article(
                url="https://a.com/1",  # type: ignore[arg-type]
                title=".",
                source_id="a",
                fetched_at=FETCHED_AT,
                summary="one",
            ),
            Article(
                url="https://a.com/2",  # type: ignore[arg-type]
                title="!",
                source_id="a",
                fetched_at=FETCHED_AT,
                summary="two",
            ),
        ]
    )

    result = deduplicate(articles)

    assert len(result.unique) == 2


def test_duplicate_records_carry_enough_context_to_debug() -> None:
    articles = [make("https://example.com/a"), make("https://example.com/a/")]

    duplicate = deduplicate(articles).duplicates[0]

    assert duplicate.url == "https://example.com/a/"
    assert duplicate.source_id == "openai"
    assert duplicate.reason == "duplicate_url"
    assert duplicate.kept_id


def test_version_numbers_prevent_a_merge() -> None:
    """Real false positive from a live run: 0.90 similarity, two different releases."""
    articles = [
        make("https://a.com/1", "sqlite-utils 4.2", summary="one"),
        make("https://a.com/2", "sqlite-utils 4.2.1", summary="two"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 2


def test_dates_in_titles_prevent_a_merge() -> None:
    """Real false positive from a live run: two monthly roundups scoring 0.91."""
    articles = [
        make("https://a.com/1", "The latest AI news we announced in June 2026", summary="one"),
        make("https://a.com/2", "The latest AI news we announced in July 2026", summary="two"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 2


def test_model_numbers_prevent_a_merge() -> None:
    articles = [
        make("https://a.com/1", "Introducing Gemini 3.5 Flash", summary="one"),
        make("https://a.com/2", "Introducing Gemini 3.7 Flash", summary="two"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 2


def test_matching_numbers_still_allow_a_merge() -> None:
    """The guard blocks differing numbers, it does not disable the similarity pass."""
    articles = [
        make("https://a.com/1", "Llama 4 is released today", summary="one"),
        make("https://a.com/2", "Llama 4 is released today.", summary="two"),
    ]

    result = deduplicate(articles)

    assert len(result.unique) == 1
    assert result.counts_by_reason() == {"similar_title": 1}
