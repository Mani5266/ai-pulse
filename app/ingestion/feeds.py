"""Feed parsing.

Pure functions: bytes in, :class:`~app.core.models.Article` list out. No network, no
filesystem, no clock beyond the ``fetched_at`` that the caller supplies — which is what
makes this stage exhaustively testable against saved feed fixtures.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import feedparser
from pydantic import ValidationError

from app.core.models import Article, Source

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(value: str | None) -> str | None:
    """Reduce a feed's HTML fragment to plain text.

    This is text extraction, not sanitisation for rendering: nothing here is ever
    inserted into a page. Feed content is treated as untrusted data throughout, and the
    LLM boundary in P5 is what defends against instructions hidden inside it.
    """
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def _to_datetime(parsed: time.struct_time | None) -> datetime | None:
    """Convert feedparser's struct_time (always UTC) to an aware datetime."""
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(parsed), tz=UTC)
    except (OverflowError, ValueError):  # pragma: no cover - malformed date fields
        return None


def _entry_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value is not None:
            return _to_datetime(value)
    return None


def _entry_content(entry: Any) -> str | None:
    """Prefer full content over the summary, when the feed offers it."""
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        value = first.get("value") if isinstance(first, dict) else None
        if isinstance(value, str):
            return value
    return None


def parse_feed(
    source: Source,
    payload: bytes,
    *,
    fetched_at: datetime,
    max_chars: int = 20_000,
) -> list[Article]:
    """Parse one feed body into articles.

    Malformed entries are skipped individually. feedparser sets ``bozo`` for almost any
    imperfection — a stray ampersand is enough — so a bozo feed with usable entries is
    still processed; only a feed with no usable entries yields nothing.
    """
    parsed = feedparser.parse(payload)

    if parsed.get("bozo") and not parsed.get("entries"):
        exception = parsed.get("bozo_exception")
        logger.warning("%s: unparseable feed: %s", source.id, exception)
        return []

    articles: list[Article] = []

    for entry in parsed.get("entries", []):
        link = entry.get("link")
        title = strip_html(entry.get("title"))
        if not link or not title:
            logger.debug("%s: skipping entry without link or title", source.id)
            continue

        summary = strip_html(entry.get("summary"))
        content = strip_html(_entry_content(entry)) or summary

        try:
            article = Article(
                url=link,
                title=title[:500],
                source_id=source.id,
                fetched_at=fetched_at,
                published_at=_entry_published(entry),
                summary=summary[:2000] if summary else None,
                content=content[:max_chars] if content else None,
            )
        except ValidationError as exc:
            logger.debug("%s: skipping invalid entry %s: %s", source.id, link, exc)
            continue

        articles.append(article)

        if len(articles) >= source.max_items_per_run:
            break

    return articles
