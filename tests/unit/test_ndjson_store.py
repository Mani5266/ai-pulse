"""NDJSON store tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.core.models import Article
from app.ingestion.normalize import enrich
from app.storage.ndjson_store import (
    append_articles,
    articles_path,
    known_content_hashes,
    known_ids,
    read_articles,
    recent_days,
)

DAY = date(2026, 8, 26)
FETCHED_AT = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)


def article(url: str, title: str = "Title") -> Article:
    return Article(
        url=url,  # type: ignore[arg-type]
        title=title,
        source_id="example",
        fetched_at=FETCHED_AT,
    )


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    written = append_articles(tmp_path, DAY, [article("https://example.com/a")])

    assert written == 1
    stored = read_articles(tmp_path, DAY)
    assert len(stored) == 1
    assert str(stored[0].url) == "https://example.com/a"
    assert stored[0].fetched_at == FETCHED_AT


def test_file_is_partitioned_by_day(tmp_path: Path) -> None:
    append_articles(tmp_path, DAY, [article("https://example.com/a")])

    assert articles_path(tmp_path, DAY) == tmp_path / "articles" / "2026-08-26.ndjson"
    assert articles_path(tmp_path, DAY).exists()


def test_reading_a_missing_day_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_articles(tmp_path, date(1999, 1, 1)) == []


def test_repeated_urls_are_not_appended_twice(tmp_path: Path) -> None:
    append_articles(tmp_path, DAY, [article("https://example.com/a")])
    written = append_articles(
        tmp_path,
        DAY,
        [article("https://example.com/a"), article("https://example.com/b")],
    )

    assert written == 1
    assert len(read_articles(tmp_path, DAY)) == 2


def test_duplicates_within_one_batch_are_collapsed(tmp_path: Path) -> None:
    written = append_articles(
        tmp_path,
        DAY,
        [article("https://example.com/a"), article("https://example.com/a")],
    )

    assert written == 1


def test_skip_ids_argument_is_honoured(tmp_path: Path) -> None:
    written = append_articles(
        tmp_path,
        DAY,
        [article("https://example.com/a")],
        skip_ids={"https://example.com/a"},
    )

    assert written == 0
    assert not articles_path(tmp_path, DAY).exists()


def test_empty_batch_writes_nothing(tmp_path: Path) -> None:
    assert append_articles(tmp_path, DAY, []) == 0
    assert not articles_path(tmp_path, DAY).exists()


def test_known_ids_and_hashes_span_days(tmp_path: Path) -> None:
    today = enrich(article("https://example.com/a"))
    yesterday = enrich(article("https://example.com/b", title="Other"))
    append_articles(tmp_path, DAY, [today])
    append_articles(tmp_path, date(2026, 8, 25), [yesterday])

    days = [DAY, date(2026, 8, 25)]

    assert known_ids(tmp_path, days) == {today.id, yesterday.id}
    assert known_content_hashes(tmp_path, days) == {today.content_hash, yesterday.content_hash}


def test_recent_days_counts_back_from_today() -> None:
    assert recent_days(DAY, 3) == [DAY, date(2026, 8, 25), date(2026, 8, 24)]


def test_enriched_records_are_keyed_by_id_not_url(tmp_path: Path) -> None:
    """A URL variant of a stored article must not be appended a second time."""
    append_articles(tmp_path, DAY, [enrich(article("https://example.com/a"))])
    written = append_articles(
        tmp_path, DAY, [enrich(article("https://www.example.com/a/?utm_source=x"))]
    )

    assert written == 0


def test_corrupt_line_does_not_lose_the_rest_of_the_day(tmp_path: Path) -> None:
    append_articles(tmp_path, DAY, [article("https://example.com/a")])
    path = articles_path(tmp_path, DAY)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"not":"an article"}\n')
    append_articles(tmp_path, DAY, [article("https://example.com/b")])

    stored = read_articles(tmp_path, DAY)

    assert {str(item.url) for item in stored} == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_records_are_written_one_per_line_with_sorted_keys(tmp_path: Path) -> None:
    """Stable serialisation means a no-op re-run produces no diff."""
    append_articles(
        tmp_path,
        DAY,
        [article("https://example.com/a"), article("https://example.com/b")],
    )

    lines = articles_path(tmp_path, DAY).read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert lines[0].startswith('{"fetched_at"')
    assert "null" not in lines[0]


def test_full_article_text_is_never_committed(tmp_path: Path) -> None:
    """Git history is immutable: committed records must stay small forever."""
    fat = Article(
        url="https://example.com/fat",  # type: ignore[arg-type]
        title="Fat article",
        source_id="example",
        fetched_at=FETCHED_AT,
        summary="s" * 4000,
        content="c" * 20000,
    )

    append_articles(tmp_path, DAY, [fat])
    line = articles_path(tmp_path, DAY).read_text(encoding="utf-8")

    assert "ccc" not in line
    assert '"content"' not in line
    assert len(line) < 1200

    stored = read_articles(tmp_path, DAY)[0]
    assert stored.content is None
    assert stored.summary is not None
    assert len(stored.summary) <= 501


def test_short_summaries_are_stored_intact(tmp_path: Path) -> None:
    article_with_summary = Article(
        url="https://example.com/short",  # type: ignore[arg-type]
        title="Short",
        source_id="example",
        fetched_at=FETCHED_AT,
        summary="A concise summary.",
    )

    append_articles(tmp_path, DAY, [article_with_summary])

    assert read_articles(tmp_path, DAY)[0].summary == "A concise summary."
