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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.briefing.models import Briefing
from app.briefing.render_telegram import render_telegram
from app.core.config import Settings
from app.delivery.telegram import API_BASE, TelegramDelivery
from app.storage.briefing_store import all_briefings

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
    """What the last run achieved, for when the answer looks wrong and you want to know why."""
    briefings = all_briefings(data_dir)
    if not briefings:
        return "No runs recorded yet."

    briefing = briefings[0]
    stats = briefing.stats
    covered = (
        briefing.covers_since.strftime("%a %d %b %H:%M UTC") if briefing.covers_since else "unknown"
    )
    return (
        f"🤖 <b>Last run</b> · {briefing.generated_at.strftime('%a %d %b %H:%M UTC')}\n\n"
        f"Covering since: {covered}\n"
        f"Feeds: {stats.feeds_ok} ok, {stats.feeds_failed} failed\n"
        f"Articles: {stats.articles} · duplicates removed: {stats.duplicates_removed}\n"
        f"Events: {stats.events} · shortlisted: {stats.events_shortlisted}\n"
        f"Model: {stats.provider} · {stats.model_calls} calls, "
        f"{stats.model_failures} failures\n"
        f"Stories published: {len(briefing.stories)}\n"
        f"Runtime: {stats.runtime_seconds:.0f}s\n"
        f"Archive: {len(briefings)} briefings"
    )


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
        if update.chat_id != self._owner:
            logger.warning("bot: ignoring a message from an unknown chat")
            return None

        command = update.text.split()[0].lower() if update.text else ""

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

    def poll_once(self) -> int:
        """Poll, answer whatever arrived, and report how many messages were handled."""
        handled = 0
        for update in self.fetch_updates():
            reply = self.handle(update)
            if reply is None:
                continue
            self._delivery.send(reply)
            handled += 1
        return handled
