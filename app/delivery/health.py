"""Degraded-run detection.

GitHub emails the owner when a scheduled workflow fails, so an outright failure is not
silent. The failure this project could not see is the one that *succeeds*: a run that exits
zero, commits its data, deploys the site, and publishes two stories instead of five. Nobody
is watching at 02:00 UTC, and a thin briefing still reads as a briefing.

This module turns that into a message. It looks at the run record the pipeline just wrote
and decides whether the run was healthy, degraded, or merely quiet.

**Quiet is not degraded, and the distinction is the whole design.** A day with three stories
because only three events cleared the shortlist is the pipeline working — the briefing is
allowed to be short, and `PLAN.md` §2.12 is explicit that a gap must never be padded. A day
with three stories because the shortlist held twenty and the model stopped answering is a
fault. Every check below therefore compares an output against the input that was available
to it, never against a fixed expectation.

The second design rule is that an alert nobody reads is worse than no alert. A permanently
dead feed must not produce a message every morning until it is background noise, so the
feed check fires on a proportion of the whole registry rather than on any single source —
slow rot is the weekly ``feeds.yml`` workflow's job, not this one's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape

from app.core.config import Settings
from app.delivery.telegram import DeliveryResult, TelegramDelivery
from app.storage.run_store import RunRecord

logger = logging.getLogger(__name__)

SUPERSEDED = "superseded by the existing briefing"
"""Not a delivery failure. The pipeline kept a better briefing and sent nothing on purpose."""

FEED_FAILURE_RATIO = 0.25
"""Alert when a quarter of the registry fails at once. One dead feed is not an incident."""


@dataclass(frozen=True, slots=True)
class Concern:
    """One thing about a run that is worth waking someone for."""

    code: str
    """Stable identifier, so a future check can count how often each fires."""

    detail: str
    """One line, written for someone reading a phone at breakfast."""


def assess(record: RunRecord, *, min_stories: int) -> list[Concern]:
    """What is wrong with this run, in the order a reader should care.

    Pure: no I/O, no settings beyond the threshold, so the judgement can be tested against
    a constructed record rather than against a live pipeline.
    """
    concerns: list[Concern] = []

    # 1. The briefing itself. Compared against what the shortlist actually offered, so a
    #    quiet day is silent and a day that had material and lost it is not.
    available = record.events_shortlisted
    if record.stories_published < min_stories <= available:
        concerns.append(
            Concern(
                code="thin_briefing",
                detail=(
                    f"{record.stories_published} stories published from "
                    f"{available} shortlisted — expected at least {min_stories}"
                ),
            )
        )
    elif record.stories_published == 0 and record.articles_in_window > 0:
        # Below the threshold above only when min_stories is 0, and worth its own message
        # either way: a run that ingested articles and published nothing is not quiet.
        concerns.append(
            Concern(
                code="nothing_published",
                detail=(f"nothing published from {record.articles_in_window} articles in window"),
            )
        )

    # 2. The model. A run with no provider still publishes, on the deterministic ranking
    #    alone — correct behaviour, and invisible to a reader who only sees missing prose.
    if record.provider == "none":
        concerns.append(
            Concern(
                code="no_model",
                detail="no model provider was available; the briefing has no prose",
            )
        )
    elif record.model_calls and record.model_failures * 2 >= record.model_calls:
        concerns.append(
            Concern(
                code="model_degraded",
                detail=(
                    f"{record.model_failures} of {record.model_calls} model calls failed "
                    f"on {record.provider}"
                ),
            )
        )
    if record.schema_violations:
        concerns.append(
            Concern(
                code="schema_violations",
                detail=(
                    f"{record.schema_violations} response(s) failed schema validation — "
                    "a prompt or a model may have changed"
                ),
            )
        )

    # 3. The feeds. A proportion, never a single source: see the module docstring.
    failed = record.feeds_failed
    total = len(record.feeds)
    if total and len(failed) >= max(2, round(total * FEED_FAILURE_RATIO)):
        names = ", ".join(feed.source_id for feed in failed[:5])
        more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
        concerns.append(
            Concern(
                code="feeds_failing",
                detail=f"{len(failed)} of {total} sources failed: {names}{more}",
            )
        )

    # 4. Delivery. The briefing is already saved, so this is a notice rather than an
    #    emergency — but a reader who got no message deserves to know one was written.
    if not record.delivered and record.delivery_error and record.delivery_error != SUPERSEDED:
        concerns.append(
            Concern(
                code="delivery_failed",
                detail=f"the briefing was saved but not delivered: {record.delivery_error}",
            )
        )

    return concerns


def format_alert(record: RunRecord, concerns: list[Concern]) -> str:
    """The message. Short, escaped, and it says where to look.

    Telegram is sent with HTML parse mode, and a feed's ``source_id`` or a provider error
    reaches this text unmodified, so everything interpolated is escaped.
    """
    lines = [f"⚠️ <b>Degraded run</b> — {escape(record.day.isoformat())}"]
    lines.extend(f"• {escape(concern.detail)}" for concern in concerns)
    lines.append("")
    lines.append("The briefing is saved either way. Send /status for the full funnel.")
    return "\n".join(lines)


def report_degraded(
    settings: Settings, record: RunRecord, *, delivery: TelegramDelivery | None = None
) -> DeliveryResult | None:
    """Send one alert if the run was degraded. Returns None when there was nothing to say.

    Never raises: an alerting path that can break the run it is watching is worse than no
    alerting at all.
    """
    if not settings.alert_on_degraded:
        return None

    concerns = assess(record, min_stories=settings.alert_min_stories)
    if not concerns:
        return None

    logger.warning(
        "run degraded: %s", "; ".join(f"{concern.code}: {concern.detail}" for concern in concerns)
    )

    owns = delivery is None
    channel = delivery or TelegramDelivery(settings)
    try:
        result = channel.send(format_alert(record, concerns))
    except Exception as exc:  # noqa: BLE001 - the alert must never break the run
        logger.warning("alert: could not be sent: %s: %s", type(exc).__name__, exc)
        return DeliveryResult(ok=False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        if owns:
            channel.close()

    if result.failed:
        logger.warning("alert: not delivered: %s", result.detail)
    return result
