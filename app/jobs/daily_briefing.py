"""Entrypoint for the daily pipeline run.

Invoked as ``python -m app.jobs.daily_briefing`` by the GitHub Actions cron and, during
development, by hand. Stages are added from P1 onward; at P0 this only proves that the
package is importable and that configuration loads.
"""

from __future__ import annotations

import logging
import sys

from app.core.config import Settings, get_settings

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
    # P1: ingest. P2: dedupe. P3: cluster. P4: score. P5: LLM. P6: deliver.
    logger.info("pipeline complete (no stages implemented yet)")
    return 0


def main() -> int:
    """Console-script entrypoint."""
    settings = get_settings()
    configure_logging(settings)
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
