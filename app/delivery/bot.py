"""The Telegram bot's inbound side: answering messages, not only sending them.

The scheduled run pushes a briefing every morning. This is the other half — asking for one
at any time and getting an answer.

Two commands, and the split between them is the whole design:

``/latest`` (or any message)
    Replies instantly from the stored briefing. No fetching, no model calls, no cost. This
    is the common case: the briefing already exists, and the reader wants to read it again.

``/refresh``
    Runs the whole pipeline now and replies with the result. Takes about two minutes and
    spends model calls, so it is a deliberate act rather than the default. A run already in
    progress is not started twice.

Polling rather than webhooks, because a webhook needs a public HTTPS endpoint — a server,
a domain, a certificate — and the entire architecture rests on needing none of those.
Long-polling costs one idle connection.

**Every inbound message is untrusted.** Anyone who finds the bot can message it, so the
chat id is checked against the configured owner before anything runs, and unknown senders
get nothing. Without that check, a stranger could trigger the pipeline and spend the day's
model budget.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

import httpx

from app.briefing.models import Briefing
from app.briefing.render_telegram import render_telegram
from app.core.config import Settings
from app.delivery.telegram import API_BASE, TelegramDelivery
from app.storage.briefing_store import all_briefings
from app.storage.run_store import compute_health, read_runs

logger = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 50
"""Long-poll duration. Telegram holds the connection open until a message arrives."""

STALE_AFTER_HOURS = 6
"""How old a stored briefing may be before the reply offers to refresh it."""

HELP_TEXT = (
    "🤖 <b>AI-PULSE</b>\n\n"
    "<b>/latest</b> — the most recent briefing, instantly\n"
    "<b>/refresh</b> — fetch everything again and rebuild it (about two minutes)\n"
    "<b>/status</b> — what the last run did\n\n"
    "Any other message returns the latest briefing."
)

GUEST_HELP = (
    "🤖 <b>AI-PULSE</b>\n\n"
    "A daily briefing on AI, built from ~25 sources and published at\n"
    "https://mani5266.github.io/ai-pulse/\n\n"
    "Send anything to read the latest one.\n"
    "Source: https://github.com/Mani5266/ai-pulse"
)

OWNER_ONLY = "That command is for the bot's owner. Send anything else for the briefing."


@dataclass(frozen=True, slots=True)
class Update:
    """One inbound message, reduced to what matters."""

    update_id: int
    chat_id: str
    text: str


def _parse_update(payload: dict[str, object]) -> Update | None:
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = message.get("text")
    update_id = payload.get("update_id")
    if not isinstance(chat, dict) or not isinstance(text, str) or not isinstance(update_id, int):
        return None
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    return Update(update_id=update_id, chat_id=str(chat_id), text=text.strip())


def _staleness_note(briefing: Briefing) -> str:
    age = datetime.now(UTC) - briefing.generated_at
    if age < timedelta(hours=STALE_AFTER_HOURS):
        return ""
    hours = age.total_seconds() / 3600
    return f"\n\n<i>This briefing is {hours:.0f} hours old. Send /refresh to rebuild it.</i>"


def latest_reply(data_dir: Path) -> str:
    """The stored briefing, rendered, with a note if it has gone stale."""
    briefings = all_briefings(data_dir)
    if not briefings:
        return "No briefing has been produced yet. Send <b>/refresh</b> to build the first one."
    briefing = briefings[0]
    return render_telegram(briefing) + _staleness_note(briefing)


def status_reply(data_dir: Path) -> str:
    """The last run, and the health of every run before it.

    Read from the run history rather than from the briefing, so a *failed* run — the case
    actually worth asking about — is reported too. A briefing cannot describe the run that
    failed to produce it.
    """
    records = read_runs(data_dir)
    if not records:
        return "No runs recorded yet. Send /refresh to make one."

    latest = records[0]
    health = compute_health(records)

    lines = [
        f"🤖 <b>Last run</b> · {latest.started_at.strftime('%a %d %b %H:%M UTC')} · "
        f"{'ok' if latest.ok else '⚠️ failed'}",
        "",
        f"Feeds: {latest.feeds_ok}/{len(latest.feeds)}",
        f"Articles: {latest.articles_fetched} fetched, {latest.articles_in_window} in window",
        f"Events: {latest.events_touched} · shortlisted {latest.events_shortlisted}",
        f"Stories: {latest.stories_published} · delivered {'yes' if latest.delivered else 'no'}",
        f"Model: {latest.provider} · {latest.model_calls} calls, {latest.model_failures} failed",
        f"Runtime: {latest.duration_seconds:.0f}s",
        "",
        f"<b>Across {health.runs} runs</b>: {health.success_rate:.0%} produced a briefing "
        f"(target 95%)",
    ]

    if latest.error:
        lines.insert(2, f"Error: {escape(latest.error[:200], quote=False)}")

    if health.failing_feeds:
        worst = list(health.failing_feeds.items())[:3]
        broken = ", ".join(f"{source_id} ({count})" for source_id, count in worst)
        lines.append(f"Feeds needing attention: {escape(broken, quote=False)}")

    return "\n".join(lines)


class BriefingBot:
    """Long-polls Telegram and answers the owner's messages."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        refresh: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._token = settings.telegram_bot_token
        self._owner = settings.telegram_chat_id
        self._refresh = refresh
        self._public = settings.public_read_only
        self._reply_gap = settings.public_reply_seconds
        self._last_reply: dict[str, float] = {}
        self._offset: int | None = None
        self._owns_client = client is None
        # Slightly longer than the poll, so the long poll is never cut off by the timeout.
        self._client = client or httpx.Client(timeout=httpx.Timeout(POLL_TIMEOUT_SECONDS + 15))
        self._delivery = TelegramDelivery(settings, client=self._client)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_updates(self) -> list[Update]:
        """One long poll. Returns whatever arrived, or nothing."""
        params: dict[str, int] = {"timeout": POLL_TIMEOUT_SECONDS}
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            response = self._client.get(f"{API_BASE}/bot{self._token}/getUpdates", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("bot: poll failed: %s: %s", type(exc).__name__, exc)
            return []

        updates: list[Update] = []
        for raw in payload.get("result", []):
            if not isinstance(raw, dict):
                continue
            update = _parse_update(raw)
            if update is None:
                continue
            # Acknowledge every update, including ones we ignore, so a message we cannot
            # parse is not re-delivered forever.
            self._offset = update.update_id + 1
            updates.append(update)
        return updates

    def handle(self, update: Update) -> str | None:
        """Work out the reply, or None if the message should be ignored.

        Anyone who finds the bot can message it, so a sender that is not the configured
        owner is answered with nothing at all — not an error, which would confirm the bot
        exists and is worth probing.
        """
        command = update.text.split()[0].lower() if update.text else ""

        if update.chat_id != self._owner:
            if not self._public:
                logger.warning("bot: ignoring a message from an unknown chat")
                return None
            return self._guest_reply(update, command)

        if command in {"/start", "/help"}:
            return HELP_TEXT
        if command == "/status":
            return status_reply(self._settings.data_dir)
        if command == "/refresh":
            if self._refresh is None:
                return "Refreshing is not available in this process."
            logger.info("bot: refresh requested")
            return self._refresh()
        return latest_reply(self._settings.data_dir)

    def _guest_reply(self, update: Update, command: str) -> str | None:
        """What a stranger gets in public mode: the briefing, and nothing that costs.

        Commands that spend the model budget or describe the pipeline's internals are
        refused rather than silently ignored, because a guest who typed ``/refresh``
        should learn why nothing happened.
        """
        if command in {"/refresh", "/status"}:
            return OWNER_ONLY

        now = time.monotonic()
        last = self._last_reply.get(update.chat_id)
        if last is not None and now - last < self._reply_gap:
            # Silence rather than a "slow down" message, which would itself be a reply
            # worth spamming for.
            logger.info("bot: rate limiting a guest chat")
            return None
        self._last_reply[update.chat_id] = now

        if command in {"/start", "/help"}:
            return GUEST_HELP
        return latest_reply(self._settings.data_dir)

    def poll_once(self) -> int:
        """Poll, answer whatever arrived, and report how many messages were handled."""
        handled = 0
        for update in self.fetch_updates():
            reply = self.handle(update)
            if reply is None:
                continue
            # Answer whoever asked. Without the override every reply would go to the
            # configured owner, so a guest's question would be answered to someone else.
            self._delivery.send(reply, chat_id=update.chat_id)
            handled += 1
        return handled
