"""Rebuild the site and the bot feed from committed data.

    python scripts/publish_site.py

No feeds, no model, no network, no writes to ``data/``. Everything published here is
derived from briefings and run records that are already in git, which is what makes it
safe to run at any time and free to run at all.

This exists because a rendering change had no way to reach the site. The only path to
Pages was the daily pipeline, so correcting a template or adding a field to ``bot.json``
meant running the whole thing — spending a day's model allowance to change some HTML, and
advancing the recency window as a side effect, which then shortened the next real run.
"""

from __future__ import annotations

import logging
import sys

from app.core.config import Settings
from app.delivery.bot_feed import build_bot_feed
from app.storage.briefing_store import build_site

logger = logging.getLogger("ai_pulse.publish")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s %(message)s")
    settings = Settings()

    pages = build_site(settings.data_dir, settings.site_dir)
    if not pages:
        logger.error("no briefings in %s; nothing to publish", settings.data_dir)
        return 1

    feed = build_bot_feed(settings.data_dir, settings.site_dir)
    logger.info("published %d pages and %s", pages, feed.name if feed else "no bot feed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
