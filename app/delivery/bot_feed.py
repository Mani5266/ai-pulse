"""The bot's answers, published as data.

A Telegram webhook needs somewhere to receive an HTTP POST, and this project has no server
and no budget for one. A Cloudflare Worker is free and always on, but it is JavaScript, and
re-implementing :mod:`app.briefing.render_telegram` there would put two renderers in one
project and guarantee they drift.

So nothing is re-implemented. The daily run renders every reply the bot can give and
publishes them to the static site as ``bot.json``. The Worker picks a field by command and
posts it verbatim: it holds no project text, no formatting, and no knowledge of what a
briefing is. Every word the bot says is still authored here, in Python, under test.

What the Worker does own is staleness. The file is written when the briefing is published
and read whenever somebody asks, so the gap between those two moments is the one thing the
publisher cannot know. ``generated_at`` is included for exactly that, and the note the
Worker appends is the counterpart of :func:`app.delivery.bot._staleness_note`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.briefing.render_telegram import render_telegram
from app.delivery.bot import GUEST_HELP, OWNER_ONLY, status_reply
from app.storage.briefing_store import all_briefings

logger = logging.getLogger(__name__)

FEED_NAME = "bot.json"

NO_BRIEFING = "No briefing has been published yet. The daily run makes the first one."
"""Not the wording the local bot uses: that one offers /refresh, which a Worker cannot do."""


def build_bot_feed(data_dir: Path, site_dir: Path) -> Path | None:
    """Write every reply the webhook bot can give. Returns the path, or None if there is
    nothing to publish yet.

    Deliberately not called from :func:`app.storage.briefing_store.build_site`, which would
    import this module and close a cycle: the bot reads briefings, and the briefing store
    would then read the bot. The pipeline calls both instead.
    """
    briefings = all_briefings(data_dir)
    if not briefings:
        logger.info("no briefing to publish for the bot")
        return None

    briefing = briefings[0]
    feed = {
        # The rendered briefing, without a staleness note: the note depends on when it is
        # read, and this file is written once and read for a day.
        "latest": render_telegram(briefing),
        "generated_at": briefing.generated_at.isoformat(),
        "day": briefing.day.isoformat(),
        "help": GUEST_HELP,
        "owner_only": OWNER_ONLY,
        "status": status_reply(data_dir),
        "no_briefing": NO_BRIEFING,
    }

    site_dir.mkdir(parents=True, exist_ok=True)
    path = site_dir / FEED_NAME
    path.write_text(
        json.dumps(feed, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("bot feed published: %s (%d characters)", path, len(feed["latest"]))
    return path
