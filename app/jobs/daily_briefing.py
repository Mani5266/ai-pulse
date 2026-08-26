"""Entrypoint for the daily pipeline run.

Invoked as ``python -m app.jobs.daily_briefing`` by the GitHub Actions cron and, during
development, by hand. Stages are added phase by phase; P1 implements ingestion and
persistence.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.core.errors import ConfigError
from app.ingestion.dedup import deduplicate
from app.ingestion.normalize import enrich_all
from app.ingestion.runner import ingest_all, summarise
from app.ingestion.sources import enabled_sources, load_sources
from app.intelligence.clustering import ClusterConfig, cluster_articles
from app.storage.event_store import latest_events, write_events
from app.storage.ndjson_store import (
    append_articles,
    known_content_hashes,
    known_ids,
    recent_days,
)

logger = logging.getLogger("ai_pulse")


def configure_logging(settings: Settings) -> None:
    """Set up structured-ish stdout logging at the configured level."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stdout,
    )


def run(settings: Settings) -> int:
    """Run one pipeline pass. Returns a process exit code."""
    logger.info(
        "pipeline start provider=%s model=%s call_budget=%d data_dir=%s",
        settings.llm_provider,
        settings.llm_model,
        settings.llm_call_budget,
        settings.data_dir,
    )

    try:
        sources = enabled_sources(load_sources())
    except ConfigError as exc:
        logger.error("source registry unusable: %s", exc)
        return 2

    logger.info("ingesting %d enabled sources", len(sources))
    results = ingest_all(sources, settings)
    stats = summarise(results)

    for result in results:
        if not result.ok:
            logger.warning("source failed: %s: %s", result.source_id, result.error)

    today = datetime.now(UTC).date()

    # Articles arrive in source-registry order, which puts first-party announcements
    # before news write-ups of them. Deduplication keeps the first copy it sees, so that
    # ordering decides which copy survives.
    articles = enrich_all(article for result in results for article in result.articles)

    memory = recent_days(today, settings.dedup_memory_days)
    deduped = deduplicate(
        articles,
        known_ids=known_ids(settings.data_dir, memory),
        known_content_hashes=known_content_hashes(settings.data_dir, memory),
        title_threshold=settings.dedup_title_threshold,
    )

    written = append_articles(settings.data_dir, today, deduped.unique)

    logger.info(
        "ingestion complete sources=%d ok=%d failed=%d articles=%d",
        stats["sources"],
        stats["ok"],
        stats["failed"],
        stats["articles"],
    )
    logger.info(
        "deduplication complete unique=%d removed=%d rate=%.1f%% stored=%d (%s)",
        len(deduped.unique),
        len(deduped.duplicates),
        deduped.duplicate_rate * 100,
        written,
        ", ".join(f"{reason}={count}" for reason, count in deduped.counts_by_reason().items())
        or "no duplicates",
    )

    event_window = list(reversed(recent_days(today, settings.event_memory_days)))
    clustered = cluster_articles(
        deduped.unique,
        existing=latest_events(settings.data_dir, event_window),
        config=ClusterConfig(threshold=settings.cluster_threshold),
    )
    write_events(settings.data_dir, today, clustered.events)

    logger.info(
        "clustering complete events=%d new=%d updated=%d multi_source=%d ratio=%.2f",
        len(clustered.events),
        len(clustered.new_event_ids),
        len(clustered.updated_event_ids),
        len(clustered.multi_source_events),
        clustered.stats()["articles_per_event"],
    )

    # P4: score. P5: LLM. P6: deliver.

    if stats["ok"] == 0:
        logger.error("every source failed; nothing to work with")
        return 1

    return 0


def main() -> int:
    """Console-script entrypoint."""
    settings = get_settings()
    configure_logging(settings)
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
