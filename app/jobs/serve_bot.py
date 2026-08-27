"""Run the bot: answer messages until stopped.

    python -m app.jobs.serve_bot

Long-polling, so no public endpoint, no webhook, no server. The process holds one idle
connection to Telegram and wakes when a message arrives.

This is the interactive half of the product. The scheduled run pushes a briefing each
morning; this answers when you ask. ``/latest`` replies from what is already stored, so
asking twice costs nothing.
"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from app.core.config import Settings, get_settings
from app.delivery.bot import BriefingBot
from app.jobs.daily_briefing import configure_logging, run

logger = logging.getLogger("ai_pulse.bot")

_running = True


def _stop(signum: int, frame: FrameType | None) -> None:
    global _running
    logger.info("bot: stopping")
    _running = False


def refresh_and_report(settings: Settings) -> str:
    """Run the whole pipeline, then describe what came out.

    The pipeline delivers the briefing itself, so this returns a short acknowledgement
    rather than repeating it. A failure is reported plainly: a bot that silently does
    nothing is worse than one that says it failed.
    """
    logger.info("bot: running the pipeline on request")
    try:
        code = run(settings)
    except Exception as exc:
        logger.exception("bot: pipeline crashed")
        return f"⚠️ The run failed: {type(exc).__name__}: {exc}"

    if code != 0:
        return f"⚠️ The run finished with exit code {code}. Send /status for the details."
    return "✅ Rebuilt. The briefing above is current."


def main() -> int:
    settings = get_settings()
    configure_logging(settings)

    if not settings.telegram_enabled:
        logger.error(
            "telegram is not configured; set AI_PULSE_TELEGRAM_BOT_TOKEN and "
            "AI_PULSE_TELEGRAM_CHAT_ID"
        )
        return 2

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # No refresh in a container: the run would write into an ephemeral layer the next
    # deploy discards, and without a model key it could replace a good briefing with a
    # worse one. See ``Settings.bot_allow_refresh``.
    refresh = (lambda: refresh_and_report(settings)) if settings.bot_allow_refresh else None
    if refresh is None:
        logger.info("bot: refresh is disabled in this process")

    bot = BriefingBot(settings, refresh=refresh)
    logger.info("bot: listening. Send /help in Telegram.")

    try:
        while _running:
            bot.poll_once()
    finally:
        bot.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
