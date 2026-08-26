"""Answer whatever is waiting, then exit.

    python -m app.jobs.poll_bot

The one-shot counterpart to ``serve_bot``. That one holds a connection open and answers
instantly, which needs a machine that is always on. This one drains the queue and exits,
so it can run on a schedule — a five-minute cron on GitHub Actions costs nothing and needs
no server, at the price of a reply arriving on the next tick rather than at once.

``/refresh`` is deliberately unavailable here. A scheduled worker has a few minutes and no
business starting a two-minute pipeline run that the daily workflow already performs.
"""

from __future__ import annotations

import logging
import sys

from app.core.config import get_settings
from app.delivery.bot import BriefingBot
from app.jobs.daily_briefing import configure_logging

logger = logging.getLogger("ai_pulse.bot")


def main() -> int:
    settings = get_settings()
    configure_logging(settings)

    if not settings.telegram_enabled:
        logger.error(
            "telegram is not configured; set AI_PULSE_TELEGRAM_BOT_TOKEN and "
            "AI_PULSE_TELEGRAM_CHAT_ID"
        )
        return 2

    bot = BriefingBot(settings)
    try:
        handled = bot.drain()
    finally:
        bot.close()

    logger.info("bot: answered %d message(s)", handled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
