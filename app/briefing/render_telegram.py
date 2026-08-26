"""Rendering a briefing as a Telegram message.

Two constraints shape everything here.

**Length.** Telegram rejects a message over 4,096 characters, and the product promises a
sixty-second read, which is shorter still. The renderer therefore trims to a budget rather
than hoping the model was brief, and the trimming is deterministic so the same briefing
always renders identically.

**Escaping.** Every string in a briefing is untrusted: the headlines come from a model that
was fed text from the open internet, and the source titles come from that text directly. In
HTML parse mode an unescaped ``<`` breaks the message, and a crafted article title could
inject markup into it. Everything is escaped on the way in, without exception.
"""

from __future__ import annotations

from html import escape

from app.briefing.models import Briefing, Story
from app.intelligence.categories import Category

TELEGRAM_MAX_CHARS = 4096
"""Hard limit imposed by the Bot API."""

BODY_BUDGET = 3600
"""Self-imposed, below the hard limit, leaving room for the header and footer. A briefing
that fills the whole allowance is no longer a sixty-second read."""

CATEGORY_ICONS: dict[Category, str] = {
    Category.MODEL_RELEASE: "🚀",
    Category.RESEARCH: "🧠",
    Category.OPEN_SOURCE: "📦",
    Category.DEVELOPER_TOOLS: "🛠",
    Category.AI_AGENTS: "🤖",
    Category.INFRASTRUCTURE: "⚙️",
    Category.FUNDING: "💰",
    Category.ACQUISITION: "🤝",
    Category.POLICY: "⚖️",
    Category.SAFETY: "🛡",
    Category.BENCHMARK: "📊",
    Category.SECURITY: "🔐",
    Category.PRODUCT: "📱",
    Category.OTHER: "•",
}


def _clean(text: str, limit: int | None = None) -> str:
    """Escape untrusted text for HTML parse mode, and optionally shorten it."""
    collapsed = " ".join(text.split())
    if limit and len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return escape(collapsed, quote=False)


def _sources_line(story: Story) -> str:
    """Sources as inline links, so every claim in the briefing is one tap from its origin."""
    if not story.sources:
        return ""
    links = [
        f'<a href="{escape(source.url, quote=True)}">{_clean(source.source_id, 24)}</a>'
        for source in story.sources
    ]
    return "   " + " · ".join(links)


def render_story(story: Story, *, index: int, detailed: bool) -> str:
    """One story.

    The lead gets full treatment; the rest are compressed, because a sixty-second briefing
    can afford depth once.
    """
    icon = CATEGORY_ICONS.get(story.category, "•")
    parts: list[str] = []

    marker = "🔥" if index == 1 else icon
    developing = " <i>(developing)</i>" if story.is_developing else ""
    parts.append(f"{marker} <b>{_clean(story.headline, 120)}</b>{developing}")

    if detailed:
        parts.append(_clean(story.what_happened, 320))
        parts.append(f"<i>Why it matters:</i> {_clean(story.why_it_matters, 260)}")
        if story.developer_impact:
            parts.append(f"<i>For developers:</i> {_clean(story.developer_impact, 200)}")
    else:
        parts.append(_clean(story.what_happened, 200))

    sources = _sources_line(story)
    if sources:
        corroboration = f" ({story.source_count} sources)" if story.source_count > 1 else ""
        parts.append(sources + corroboration)

    return "\n".join(parts)


def render_header(briefing: Briefing) -> str:
    """The header states the window covered, not merely the date.

    A briefing headed "Wednesday" that in fact reports four days of news is lying to its
    reader, and after a missed run that is exactly what it would be.
    """
    covered = ""
    if briefing.covers_since:
        covered = f" · since {briefing.covers_since.strftime('%a %d %b %H:%M UTC')}"
    return (
        f"🤖 <b>AI-PULSE</b> · {briefing.day.strftime('%A, %d %B %Y')}{covered}\n"
        f"<i>{len(briefing.stories)} stories from "
        f"{briefing.stats.articles} articles</i>"
    )


def render_footer(briefing: Briefing) -> str:
    """The run's own numbers.

    Included deliberately: a briefing that silently degrades is worse than one that says it
    degraded, and a failed feed or a skipped model call is exactly what the reader needs to
    know before trusting a quiet day.
    """
    stats = briefing.stats
    bits = [f"{stats.feeds_ok} feeds"]
    if stats.feeds_failed:
        bits.append(f"{stats.feeds_failed} failed")
    bits.append(f"{stats.events} events")
    if stats.model_failures:
        bits.append(f"{stats.model_failures} model failures")
    bits.append(f"{stats.runtime_seconds:.0f}s")
    return "<i>" + " · ".join(_clean(bit) for bit in bits) + "</i>"


def render_telegram(briefing: Briefing) -> str:
    """Render the whole briefing, trimmed to fit.

    Stories are added while there is room, so a long lead costs the tail rather than
    breaking the message. The result always fits the Bot API limit.
    """
    if briefing.is_empty:
        return (
            f"{render_header(briefing)}\n\n"
            "No story could be verified today. Nothing is being published rather than "
            "publishing something unsupported.\n\n"
            f"{render_footer(briefing)}"
        )

    header = render_header(briefing)
    footer = render_footer(briefing)

    body_parts: list[str] = []
    used = 0

    for index, story in enumerate(briefing.stories, start=1):
        rendered = render_story(story, index=index, detailed=index == 1)
        if used + len(rendered) > BODY_BUDGET and body_parts:
            break
        body_parts.append(rendered)
        used += len(rendered) + 2

    message = f"{header}\n\n" + "\n\n".join(body_parts) + f"\n\n{footer}"

    if len(message) > TELEGRAM_MAX_CHARS:
        # Belt and braces: never hand the API something it will reject.
        message = message[: TELEGRAM_MAX_CHARS - 1].rstrip() + "…"

    return message
