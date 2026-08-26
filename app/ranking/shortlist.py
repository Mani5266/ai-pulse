"""Shortlisting: from every event of the day to the handful the model will see.

This is the stage that makes a zero-cost run possible. Roughly 500 articles become roughly
500 events, and the model must see at most 20 of them — so the cut has to happen here, in
code, before the first call.

A pure top-N by score is not enough. On a typical day the research category alone produces
eighty events, and a briefing consisting of eight arXiv papers is a worse briefing than one
covering a release, a paper, a policy story and an outage, even if the papers scored
marginally higher. So the shortlist takes the best of each category in turn rather than
draining the highest-scoring one first.

**Corroborated events bypass the cap.** An event covered by two or more independent
sources is the strongest signal of importance available without a model, and the cap exists
to stop *volume* dominating, which corroboration is the opposite of. On live data the cap
was pushing out exactly the wrong events: a Gemma 4 release covered by three sources was
dropped in favour of four single-source blog posts that happened to share its category.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.intelligence.categories import Category
from app.ranking.scoring import ScoredEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_CATEGORY = 4


@dataclass(frozen=True, slots=True)
class Shortlist:
    """The events selected for the model, and what it cost to get there."""

    selected: list[ScoredEvent]
    considered: int
    dropped_by_category_cap: int
    corroborated: int = 0

    def stats(self) -> dict[str, int | float]:
        return {
            "considered": self.considered,
            "selected": len(self.selected),
            "dropped_by_category_cap": self.dropped_by_category_cap,
            "corroborated": self.corroborated,
            "top_score": round(self.selected[0].score, 3) if self.selected else 0.0,
            "cut_off_score": round(self.selected[-1].score, 3) if self.selected else 0.0,
        }

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.selected:
            key = item.event.category.value
            counts[key] = counts.get(key, 0) + 1
        return counts


def build_shortlist(
    scored: Sequence[ScoredEvent],
    *,
    limit: int,
    max_per_category: int = DEFAULT_MAX_PER_CATEGORY,
) -> Shortlist:
    """Select at most ``limit`` events, no more than ``max_per_category`` from any one.

    ``scored`` must already be sorted best-first. Selection walks it in order, so within a
    category the best events are taken; the cap only decides how many.

    If the caps leave the shortlist short of ``limit`` — a quiet day, or one dominated by a
    single category — the remaining slots are filled by score, ignoring the caps. An empty
    slot helps nobody.
    """
    selected: list[ScoredEvent] = []
    per_category: dict[Category, int] = {}
    overflow: list[ScoredEvent] = []

    for item in scored:
        if len(selected) >= limit:
            break
        category = item.event.category
        corroborated = item.event.source_count > 1
        if not corroborated and per_category.get(category, 0) >= max_per_category:
            overflow.append(item)
            continue
        per_category[category] = per_category.get(category, 0) + 1
        selected.append(item)

    dropped = len(overflow)

    for item in overflow:
        if len(selected) >= limit:
            break
        selected.append(item)
        dropped -= 1

    # Restore global score order: the category walk can interleave a lower-scoring event
    # ahead of a higher-scoring one, and the briefing leads with the biggest story.
    selected.sort(
        key=lambda item: (
            -item.score,
            -item.event.source_count,
            -item.event.last_updated.timestamp(),
            item.event.id,
        )
    )

    shortlist = Shortlist(
        selected=selected,
        considered=len(scored),
        dropped_by_category_cap=max(dropped, 0),
        corroborated=sum(1 for item in selected if item.event.source_count > 1),
    )
    logger.info(
        "shortlist: %d of %d events (%s)",
        len(selected),
        len(scored),
        ", ".join(f"{name}={count}" for name, count in sorted(shortlist.categories().items())),
    )
    return shortlist
