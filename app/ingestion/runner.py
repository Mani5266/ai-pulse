"""Ingestion orchestration.

One dead feed must never end a run, so every failure is captured as a
:class:`~app.core.models.FeedResult` value rather than raised. The run statistics in P10
are built from exactly these records.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import AIPulseError, UnsafeURLError
from app.core.models import FeedResult, Source
from app.ingestion.feeds import parse_feed
from app.ingestion.fetcher import SafeFetcher

logger = logging.getLogger(__name__)


FETCH_RETRY_DELAY_SECONDS = 2.0
"""Pause before the single retry. Long enough to clear a transient refusal, short enough
that a dead feed costs two seconds rather than a minute."""


def ingest_source(
    fetcher: SafeFetcher, source: Source, settings: Settings, *, retries: int = 1
) -> FeedResult:
    """Fetch and parse one source. Never raises.

    One retry, because feed failures are frequently transient: the same URL that returns
    403 or times out on one attempt serves 50 entries on the next. Two sources of 25 failed
    on a live run and both had succeeded minutes earlier. A silently missing feed is worse
    than a slow run, because it costs coverage without reporting anything.
    """
    started = time.monotonic()
    fetched_at = datetime.now(UTC)

    response = None
    last_error: AIPulseError | None = None

    for attempt in range(retries + 1):
        if attempt:
            time.sleep(FETCH_RETRY_DELAY_SECONDS)
        try:
            response = fetcher.get(str(source.feed_url))
            break
        except UnsafeURLError as exc:
            # A refusal by the SSRF guard is a decision, not a hiccup. Retrying it would
            # be retrying a security control until it relents.
            logger.warning("%s: rejected: %s", source.id, exc)
            last_error = exc
            break
        except AIPulseError as exc:
            logger.warning(
                "%s: fetch failed (attempt %d of %d): %s", source.id, attempt + 1, retries + 1, exc
            )
            last_error = exc

    if response is None:
        return FeedResult(
            source_id=source.id,
            ok=False,
            error=f"{type(last_error).__name__}: {last_error}",
            duration_seconds=round(time.monotonic() - started, 3),
            fetched_at=fetched_at,
        )

    try:
        articles = parse_feed(
            source,
            response.content,
            fetched_at=fetched_at,
            max_chars=settings.max_article_chars,
        )
    # Deliberately broad: a parser crash in one feed must not end the run.
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: parse failed: %s", source.id, exc)
        return FeedResult(
            source_id=source.id,
            ok=False,
            error=f"parse: {type(exc).__name__}: {exc}",
            http_status=response.status_code,
            duration_seconds=round(time.monotonic() - started, 3),
            fetched_at=fetched_at,
        )

    duration = round(time.monotonic() - started, 3)
    logger.info("%s: %d articles in %.2fs", source.id, len(articles), duration)

    return FeedResult(
        source_id=source.id,
        ok=True,
        articles=articles,
        http_status=response.status_code,
        duration_seconds=duration,
        fetched_at=fetched_at,
    )


def ingest_all(
    sources: Sequence[Source],
    settings: Settings,
    *,
    fetcher: SafeFetcher | None = None,
) -> list[FeedResult]:
    """Fetch every source in order, collecting successes and failures alike."""
    if fetcher is not None:
        return [ingest_source(fetcher, source, settings) for source in sources]

    with SafeFetcher(settings) as owned:
        return [ingest_source(owned, source, settings) for source in sources]


def summarise(results: Sequence[FeedResult]) -> dict[str, int]:
    """Counts for the run log. The full breakdown becomes the P10 dashboard."""
    return {
        "sources": len(results),
        "ok": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
        "articles": sum(result.article_count for result in results),
    }
