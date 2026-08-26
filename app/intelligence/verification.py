"""Claim verification.

The stage that makes "evidence-backed" a fact about the output rather than a word in the
README.

**The split.** The model extracts claims and says which documents assert each one. This
module assigns the label, in code, by counting *independent sources*. That division is
deliberate and is the same one the ranking formula uses: a model asked "is this verified?"
answers confidently in either direction and cannot be checked, while a model asked "which
of these documents says it?" produces an answer that can be compared against the documents
and that a reader can click through and check for themselves.

**What the labels mean.**

``VERIFIED``
    Two or more independent sources assert it. Not "true" — corroborated. Two outlets can
    both be wrong, and both repeating a press release is not two observations. The label
    claims exactly what it can support.

``PARTIALLY_VERIFIED``
    Corroborated by more than one source, but at least one other source disagrees.

``UNVERIFIED``
    One source, or none that the model could attribute. The common case, and it is not a
    criticism: a lab announcing its own model is the only source that can, and that is
    fine as long as the briefing says so rather than implying more.

``CONTRADICTED``
    Sources disagree and no majority supports it. The most valuable label in the set, and
    the reason this stage exists at all.

**Attribution is not taken on trust.** A source id the model returns is only counted if it
actually belongs to the event, so a hallucinated or misspelled attribution is discarded
rather than inflating a count. This matters: the failure mode of a verification feature is
verifying things by making up witnesses.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.llm.schemas import ExtractedClaim

logger = logging.getLogger(__name__)

CORROBORATION_THRESHOLD = 2
"""Independent sources needed before a claim counts as corroborated."""


class VerificationStatus(StrEnum):
    """How well the sources back a claim."""

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"


class VerifiedClaim(BaseModel):
    """A claim, its attributed sources, and the label computed from them."""

    model_config = ConfigDict(frozen=True)

    text: str
    status: VerificationStatus
    supported_by: list[str] = Field(default_factory=list)
    contradicted_by: list[str] = Field(default_factory=list)

    @property
    def support_count(self) -> int:
        return len(self.supported_by)

    @property
    def is_corroborated(self) -> bool:
        return self.status is VerificationStatus.VERIFIED


def _known_only(ids: Sequence[str], event_sources: set[str]) -> list[str]:
    """Keep only attributions to sources the event actually has.

    A model that invents or misspells a source id would otherwise manufacture
    corroboration, which is precisely the failure this feature must not have.
    """
    seen: list[str] = []
    for source_id in ids:
        normalised = source_id.strip().lower()
        if normalised in event_sources and normalised not in seen:
            seen.append(normalised)
    return seen


def classify_claim(claim: ExtractedClaim, event_sources: set[str]) -> VerifiedClaim:
    """Assign a status to one claim, deterministically, from its attributions."""
    supported = _known_only(claim.supported_by, event_sources)
    contradicted = _known_only(claim.contradicted_by, event_sources)

    dropped = (len(claim.supported_by) - len(supported)) + (
        len(claim.contradicted_by) - len(contradicted)
    )
    if dropped:
        logger.debug("verification: ignored %d attribution(s) to unknown sources", dropped)

    if contradicted and len(supported) < CORROBORATION_THRESHOLD:
        status = VerificationStatus.CONTRADICTED
    elif contradicted:
        status = VerificationStatus.PARTIALLY_VERIFIED
    elif len(supported) >= CORROBORATION_THRESHOLD:
        status = VerificationStatus.VERIFIED
    else:
        status = VerificationStatus.UNVERIFIED

    return VerifiedClaim(
        text=claim.text,
        status=status,
        supported_by=supported,
        contradicted_by=contradicted,
    )


def classify_claims(
    claims: Sequence[ExtractedClaim], event_sources: Sequence[str]
) -> list[VerifiedClaim]:
    """Classify every claim for one event, best-supported first."""
    known = {source_id.strip().lower() for source_id in event_sources}
    verified = [classify_claim(claim, known) for claim in claims]

    order = {
        VerificationStatus.CONTRADICTED: 0,
        VerificationStatus.VERIFIED: 1,
        VerificationStatus.PARTIALLY_VERIFIED: 2,
        VerificationStatus.UNVERIFIED: 3,
    }
    # Contradictions lead: a disagreement between sources is the most useful thing this
    # stage can tell a reader, and burying it under three corroborated claims wastes it.
    verified.sort(key=lambda claim: (order[claim.status], -claim.support_count))
    return verified


def summarise_claims(claims: Sequence[VerifiedClaim]) -> dict[str, int]:
    """Counts by status, for the run log and the P9 evaluation."""
    counts = dict.fromkeys((status.value for status in VerificationStatus), 0)
    for claim in claims:
        counts[claim.status.value] += 1
    counts["total"] = len(claims)
    return counts
