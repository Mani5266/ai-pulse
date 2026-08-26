"""Deduplication against stored history.

The failure this prevents is the one a reader would notice first: a feed that keeps
listing last week's post, presented as news every morning.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.core.models import Article
from app.ingestion.dedup import deduplicate
from app.ingestion.normalize import enrich_all
from app.storage.ndjson_store import (
    append_articles,
    known_content_hashes,
    known_ids,
    recent_days,
)

TODAY = date(2026, 8, 26)
YESTERDAY = date(2026, 8, 25)
FETCHED_AT = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)


def make(url: str, title: str, summary: str | None = None) -> Article:
    return Article(
        url=url,  # type: ignore[arg-type]
        title=title,
        source_id="example",
        fetched_at=FETCHED_AT,
        summary=summary,
    )


def run_dedup(data_dir: Path, articles: list[Article], memory_days: int = 7) -> list[Article]:
    memory = recent_days(TODAY, memory_days)
    result = deduplicate(
        enrich_all(articles),
        known_ids=known_ids(data_dir, memory),
        known_content_hashes=known_content_hashes(data_dir, memory),
    )
    append_articles(data_dir, TODAY, result.unique)
    return result.unique


def test_an_article_stored_yesterday_is_not_news_today(tmp_path: Path) -> None:
    append_articles(tmp_path, YESTERDAY, enrich_all([make("https://a.com/x", "Model X ships")]))

    fresh = run_dedup(
        tmp_path,
        [make("https://a.com/x", "Model X ships"), make("https://a.com/y", "Model Y ships")],
    )

    assert [article.title for article in fresh] == ["Model Y ships"]


def test_a_url_variant_of_yesterdays_article_is_also_suppressed(tmp_path: Path) -> None:
    append_articles(tmp_path, YESTERDAY, enrich_all([make("https://a.com/x", "Model X ships")]))

    fresh = run_dedup(tmp_path, [make("https://www.a.com/x/?utm_source=news", "Model X ships")])

    assert fresh == []


def test_a_syndicated_copy_of_yesterdays_story_is_suppressed(tmp_path: Path) -> None:
    append_articles(
        tmp_path,
        YESTERDAY,
        enrich_all([make("https://origin.com/x", "Model X ships", "Details.")]),
    )

    fresh = run_dedup(tmp_path, [make("https://elsewhere.com/y", "Model X ships", "Details.")])

    assert fresh == []


def test_memory_window_is_bounded(tmp_path: Path) -> None:
    """Outside the window an old story can legitimately resurface."""
    old = date(2026, 1, 1)
    append_articles(tmp_path, old, enrich_all([make("https://a.com/x", "Model X ships")]))

    fresh = run_dedup(tmp_path, [make("https://a.com/x", "Model X ships")], memory_days=7)

    assert len(fresh) == 1


def test_rerunning_the_same_day_stores_nothing_new(tmp_path: Path) -> None:
    articles = [make("https://a.com/x", "Model X ships"), make("https://a.com/y", "Model Y")]

    first = run_dedup(tmp_path, articles)
    second = run_dedup(tmp_path, articles)

    assert len(first) == 2
    assert second == []
