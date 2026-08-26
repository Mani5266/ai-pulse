"""NDJSON persistence.

Records are stored one JSON object per line, partitioned by UTC date::

    data/articles/2026-08-26.ndjson

Why NDJSON committed to git rather than a database file:

- A committed SQLite binary bloats the repository and produces unreadable diffs.
- Line-oriented JSON appends cleanly and diffs as added lines, so the git history of
  ``data/`` *is* the intelligence timeline the product promises.
- Keys are written sorted, so a re-run that changes nothing produces no diff.

A SQLite database for local analysis is rebuilt from these files on demand and is
gitignored.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from app.core.models import Article

logger = logging.getLogger(__name__)

ARTICLES_DIR = "articles"


def articles_path(data_dir: Path, day: date) -> Path:
    """Path of the article file for one UTC day."""
    return data_dir / ARTICLES_DIR / f"{day.isoformat()}.ndjson"


PERSISTED_SUMMARY_CHARS = 500
"""Summaries are trimmed on the way to disk. See :func:`_serialise`."""


def _serialise(article: Article) -> str:
    """One record as a single line, with sorted keys and no None padding.

    Full article text is **not** persisted, and the summary is trimmed. The reason is
    that git history is immutable: committing ~2 MB of feed text a day would add roughly
    700 MB a year to the repository, and deleting the files later would not shrink it,
    because the blobs stay in history forever.

    Full text is not lost, it is simply not *committed*. It lives in memory for the
    duration of a run, which is when clustering and the LLM need it. What is persisted is
    what later days need: identity, provenance, timing, and enough text to recognise the
    story again.

    Dropping None keys keeps records small and lets later phases add fields without
    rewriting earlier files.
    """
    payload = article.model_dump(mode="json", exclude_none=True)
    payload.pop("content", None)
    summary = payload.get("summary")
    if isinstance(summary, str) and len(summary) > PERSISTED_SUMMARY_CHARS:
        payload["summary"] = summary[:PERSISTED_SUMMARY_CHARS].rstrip() + "…"
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def read_articles(data_dir: Path, day: date) -> list[Article]:
    """Read one day's articles. A missing file is an empty day, not an error."""
    path = articles_path(data_dir, day)
    if not path.exists():
        return []

    articles: list[Article] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                articles.append(Article.model_validate_json(stripped))
            except ValidationError as exc:
                # A corrupt line must not lose the rest of the day's history.
                logger.warning("%s:%d: skipping unreadable record: %s", path, line_number, exc)
    return articles


def known_urls(data_dir: Path, days: Iterable[date]) -> set[str]:
    """Every article URL already stored across the given days.

    P1 uses this to avoid re-appending items that a feed still lists. Real deduplication,
    across sources and across URL variants, arrives in P2.
    """
    return {str(article.url) for day in days for article in read_articles(data_dir, day)}


def append_articles(
    data_dir: Path,
    day: date,
    articles: Sequence[Article],
    *,
    skip_urls: set[str] | None = None,
) -> int:
    """Append articles to one day's file, skipping URLs already present.

    Returns the number of records actually written. Writing is append-only: a run never
    rewrites history, so a re-run is safe.
    """
    if not articles:
        return 0

    path = articles_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)

    seen = set(skip_urls or ())
    seen.update(str(article.url) for article in read_articles(data_dir, day))

    lines: list[str] = []
    for article in articles:
        url = str(article.url)
        if url in seen:
            continue
        seen.add(url)
        lines.append(_serialise(article))

    if not lines:
        return 0

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

    return len(lines)
