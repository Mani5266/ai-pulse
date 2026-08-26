"""Telegram delivery.

Ordering matters here and is deliberate: the briefing is **persisted before it is sent**.
Delivery is the one stage that depends on somebody else's server being up, so a failure
there must cost nothing. The briefing already exists on disk, the static site already has
it, and a failed send is retried on the next run rather than losing a day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Outcome of one delivery attempt. A value, never an exception."""

    ok: bool
    detail: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


class TelegramDelivery:
    """Sends a briefing to one chat.

    Configuration is optional throughout: a missing token or chat id is reported, not
    raised, so a fresh clone runs end to end and simply does not deliver.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._enabled = settings.telegram_enabled
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, message: str) -> DeliveryResult:
        """Send one message. Never raises."""
        if not self._enabled:
            return DeliveryResult(ok=False, detail="telegram not configured")
        if not message.strip():
            return DeliveryResult(ok=False, detail="refusing to send an empty message")

        try:
            response = self._client.post(
                f"{API_BASE}/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    # The briefing already links its sources; previews would bury them
                    # under a wall of thumbnails.
                    "link_preview_options": {"is_disabled": True},
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("telegram: %s: %s", type(exc).__name__, exc)
            return DeliveryResult(ok=False, detail=f"{type(exc).__name__}: {exc}")

        if response.status_code != 200:
            # Telegram explains rejections in the body, and the reason is almost always
            # actionable: bad HTML, message too long, bot blocked.
            detail = response.text[:300]
            logger.warning("telegram: HTTP %d: %s", response.status_code, detail)
            return DeliveryResult(ok=False, detail=f"HTTP {response.status_code}: {detail}")

        logger.info("telegram: delivered %d characters", len(message))
        return DeliveryResult(ok=True)
