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
from app.ingestion.runner import ingest_all, summarise
from app.ingestion.sources import enabled_sources, load_sources
from app.storage.ndjson_store import append_articles

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
    articles = [article for result in results for article in result.articles]
    written = append_articles(settings.data_dir, today, articles)

    logger.info(
        "ingestion complete sources=%d ok=%d failed=%d articles=%d new=%d",
        stats["sources"],
        stats["ok"],
        stats["failed"],
        stats["articles"],
        written,
    )

    # P2: dedupe. P3: cluster. P4: score. P5: LLM. P6: deliver.

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
