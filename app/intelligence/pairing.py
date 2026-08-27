"""Candidate pairs for duplicate adjudication.

Clustering in P3 is precision-tuned and under-clusters on purpose: two outlets describing
one development in genuinely different words stay two events unless they name the same
model, lab or version. `PLAN.md` §2.9 argues that trade — a false merge puts two unrelated
stories under one headline and the reader sees it, while a missed merge only costs a slot.

It does cost a slot, though, and on 27 August it cost two of five. The same OpenAI incident
was published twice from two outlets, neither naming a shared version, so nothing in the
string-matching path could have merged them. That judgement is semantic, and this module
does not attempt it: it decides only *which pairs are worth asking about*, and the model
answers.

Two things make that affordable.

**The n² problem is bounded before it starts.** Pairs are drawn from the shortlist — the
twenty events that could actually reach a briefing — not from the several hundred the run
ranked. Twenty events is 190 pairs, still far too many to send anywhere, so the band below
filters them to a handful and the caller takes the best few.

**The gates protect a merge, not a similarity ceiling.** The first version of this module
refused pairs scoring above the clustering threshold, on the theory that they would already
be one event. That was wrong twice over. Every pair reaching here is by construction one
that clustering declined to merge, so the ceiling protected against nothing — and it
compared *this* module's blend against *clustering's* threshold, two different formulas,
which excluded the exact pair the module was written for at 0.688 against a 0.45 ceiling.
What keeps a merge safe is the explicit gates below, each replicating a decision clustering
makes for a stated reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import combinations

from app.core.models import Event
from app.intelligence.entities import (
    DECISIVE_WEIGHT,
    has_conflicting_version,
    strongest_shared,
    weighted_overlap,
)
from app.intelligence.similarity import signatures_agree, title_similarity

logger = logging.getLogger(__name__)

SIMILARITY_FLOOR = 0.12
"""Below this, two events share nothing worth a model call.

Deliberately low. The pair this module exists for scored under 0.2 on wording — that is
what it means for two outlets to describe one event in different words — so a floor tuned
to look respectable would have excluded the only case anybody complained about."""

ENTITY_WEIGHT = 0.5
"""How much shared entities count against shared wording.

Higher than clustering's blend, because wording is precisely what has already failed by
the time a pair reaches here. Two events naming the same specific product are worth asking
about even when the headlines read nothing alike."""

GATE_TITLE_SIMILARITY = 0.35
"""Wording strong enough to carry a pair with no entity evidence at all.

Below clustering's own gate, because a pair only reaches this module after clustering has
already declined it — insisting on the same evidence twice would guarantee an empty list."""

CORROBORATING_ENTITIES = 2
"""Shared entities that together count as evidence when none of them is decisive.

Measured, not chosen. On live data the real duplicate — two outlets on one OpenAI incident
— shared ``org:openai`` and ``org:huggingface``: two organisations, neither decisive on its
own, and wording of 0.195. Three unrelated stories shared exactly one generic entity,
``term:url``, with wording of 0.04. No threshold on entity *weight* separates those two
cases: the junk entity scores 0.6 and the real ones score 0.25. The count does."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """Two events that might be one, and how strongly the cheap signals agree."""

    left: Event
    right: Event
    similarity: float

    @property
    def key(self) -> tuple[str, str]:
        """Stable identity for the pair, order-independent."""
        return tuple(sorted((self.left.id, self.right.id)))  # type: ignore[return-value]


def _blended(left: Event, right: Event) -> float:
    """One number for how alike two events look, before any model sees them."""
    wording = title_similarity(left.canonical_title, right.canonical_title)
    entities = weighted_overlap(frozenset(left.entities), frozenset(right.entities))
    return ENTITY_WEIGHT * entities + (1.0 - ENTITY_WEIGHT) * wording


def candidate_pairs(
    events: list[Event],
    *,
    floor: float = SIMILARITY_FLOOR,
    limit: int = 5,
) -> list[Candidate]:
    """The pairs worth spending a model call on, strongest first.

        Three filters run before scoring, and each one exists to protect a decision made
        earlier in the pipeline rather than to save a call:

        - **Conflicting versions never pair.** "Gemma 4" and "Gemma 3" are two releases, and
          no amount of similar wording makes them one. This is clustering's rule, applied here
          so adjudication cannot undo it.
        - **Disagreeing identity signatures never pair.** Same reasoning: a differing number,
          version or month is the whole difference between two monthly roundups.
        - **Different categories never pair.** A model release and a funding round are not the
          same development even when both name the same company, and the category came from
          the pipeline rather than from a guess.

    A fourth filter is arithmetic rather than policy. ``weighted_overlap`` reports total
        agreement whenever two events share their whole entity set, however thin that set is,
        and thin sets are common: on live data three unrelated stories — a Tailscale tool, a
        GitHub outage tracker and a visa policy change — each carried ``term:url`` and nothing
        else, so all three paired with each other at 0.5. That would have spent three of five
        calls on nonsense and invited a model to delete one of them.

        So a pair must carry evidence of one of three kinds: a decisive shared entity, two
        shared entities that corroborate each other, or wording that agrees on its own.
    """
    scored: list[Candidate] = []

    for left, right in combinations(events, 2):
        if left.category is not right.category:
            continue

        left_entities, right_entities = frozenset(left.entities), frozenset(right.entities)
        if has_conflicting_version(left_entities, right_entities):
            continue
        if not signatures_agree(left.canonical_title, right.canonical_title):
            continue

        wording = title_similarity(left.canonical_title, right.canonical_title)
        shared = left_entities & right_entities
        if not (
            strongest_shared(left_entities, right_entities) >= DECISIVE_WEIGHT
            or len(shared) >= CORROBORATING_ENTITIES
            or wording >= GATE_TITLE_SIMILARITY
        ):
            continue

        similarity = _blended(left, right)
        if similarity >= floor:
            scored.append(Candidate(left=left, right=right, similarity=round(similarity, 3)))

    scored.sort(key=lambda candidate: candidate.similarity, reverse=True)

    if len(scored) > limit:
        logger.info("%d candidate pairs found; adjudicating the %d strongest", len(scored), limit)
    return scored[:limit]


def merge_events(keep: Event, drop: Event) -> Event:
    """Fold one event into another, keeping the record the ranking already preferred.

    ``keep`` is the higher-scoring of the two, so its id, title and category survive: the
    briefing was going to publish that wording anyway, and changing it here would make the
    merge visible as a rewrite rather than as a story that simply has more sources.

    Everything countable is unioned, because that is the point. The merged event carries
    both outlets, which is what makes it *look* corroborated to every stage downstream —
    and it genuinely is. The dates widen in both directions so a timeline still spans the
    whole development.
    """
    return keep.model_copy(
        update={
            "article_ids": _union(keep.article_ids, drop.article_ids),
            "source_ids": _union(keep.source_ids, drop.source_ids),
            "entities": _union(keep.entities, drop.entities),
            "first_seen": min(keep.first_seen, drop.first_seen),
            "last_updated": max(keep.last_updated, drop.last_updated),
        }
    )


def _union(first: list[str], second: list[str]) -> list[str]:
    """Both lists, in order, without repeats. Order is stable so a re-run diffs clean."""
    seen: dict[str, None] = dict.fromkeys(first)
    seen.update(dict.fromkeys(second))
    return list(seen)
