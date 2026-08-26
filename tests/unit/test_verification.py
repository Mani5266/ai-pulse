"""Claim verification: labels computed from attribution, in code.

The defining property under test is that the model never assigns the label. It says which
documents carry a claim; this module counts independent sources and decides. A model asked
"is this verified?" answers confidently either way and cannot be checked; an attribution
can be compared against the documents, and a reader can click through.
"""

from __future__ import annotations

from app.intelligence.verification import (
    CORROBORATION_THRESHOLD,
    VerificationStatus,
    classify_claim,
    classify_claims,
    summarise_claims,
)
from app.llm.schemas import ExtractedClaim

SOURCES = ["google-deepmind", "huggingface-blog", "the-verge-ai", "ollama"]


def claim(
    text: str = "Gemma 4 has 12 billion parameters",
    *,
    supported: list[str] | None = None,
    contradicted: list[str] | None = None,
) -> ExtractedClaim:
    return ExtractedClaim(
        text=text,
        supported_by=supported or [],
        contradicted_by=contradicted or [],
    )


# --- the labels ---------------------------------------------------------------


def test_two_independent_sources_corroborate() -> None:
    result = classify_claim(claim(supported=["google-deepmind", "huggingface-blog"]), set(SOURCES))

    assert result.status is VerificationStatus.VERIFIED
    assert result.is_corroborated is True


def test_one_source_is_unverified_not_false() -> None:
    """A lab announcing its own model is the only source that can. That is fine, as long
    as the briefing says so rather than implying more."""
    result = classify_claim(claim(supported=["google-deepmind"]), set(SOURCES))

    assert result.status is VerificationStatus.UNVERIFIED


def test_no_attribution_is_unverified() -> None:
    result = classify_claim(claim(), set(SOURCES))

    assert result.status is VerificationStatus.UNVERIFIED
    assert result.supported_by == []


def test_disagreement_without_corroboration_is_contradicted() -> None:
    result = classify_claim(
        claim(supported=["ollama"], contradicted=["the-verge-ai"]), set(SOURCES)
    )

    assert result.status is VerificationStatus.CONTRADICTED


def test_disagreement_against_corroboration_is_partial() -> None:
    result = classify_claim(
        claim(
            supported=["google-deepmind", "huggingface-blog"],
            contradicted=["the-verge-ai"],
        ),
        set(SOURCES),
    )

    assert result.status is VerificationStatus.PARTIALLY_VERIFIED


def test_the_threshold_is_two_independent_sources() -> None:
    assert CORROBORATION_THRESHOLD == 2


# --- attribution is not taken on trust ----------------------------------------


def test_an_invented_source_is_not_counted() -> None:
    """The failure mode of a verification feature is verifying by inventing witnesses."""
    result = classify_claim(
        claim(supported=["google-deepmind", "reuters", "the-economist"]), set(SOURCES)
    )

    assert result.supported_by == ["google-deepmind"]
    assert result.status is VerificationStatus.UNVERIFIED


def test_a_hallucinated_source_cannot_manufacture_corroboration() -> None:
    result = classify_claim(claim(supported=["made-up-one", "made-up-two"]), set(SOURCES))

    assert result.supported_by == []
    assert result.status is VerificationStatus.UNVERIFIED


def test_attribution_is_case_insensitive() -> None:
    result = classify_claim(claim(supported=["Google-DeepMind", "HUGGINGFACE-BLOG"]), set(SOURCES))

    assert result.status is VerificationStatus.VERIFIED


def test_the_same_source_twice_is_one_source() -> None:
    """Two links from one publisher are not two observations."""
    result = classify_claim(claim(supported=["google-deepmind", "google-deepmind"]), set(SOURCES))

    assert result.supported_by == ["google-deepmind"]
    assert result.status is VerificationStatus.UNVERIFIED


def test_an_invented_contradiction_is_also_discarded() -> None:
    result = classify_claim(
        claim(supported=["google-deepmind"], contradicted=["nonexistent-outlet"]),
        set(SOURCES),
    )

    assert result.contradicted_by == []
    assert result.status is VerificationStatus.UNVERIFIED


# --- ordering and reporting ---------------------------------------------------


def test_contradictions_are_listed_first() -> None:
    """A disagreement between sources is the most useful thing this stage produces, and
    burying it under three corroborated claims wastes it."""
    claims = classify_claims(
        [
            claim("Corroborated", supported=["google-deepmind", "ollama"]),
            claim("Single", supported=["ollama"]),
            claim("Disputed", supported=["ollama"], contradicted=["the-verge-ai"]),
        ],
        SOURCES,
    )

    assert claims[0].status is VerificationStatus.CONTRADICTED
    assert claims[1].status is VerificationStatus.VERIFIED


def test_better_supported_claims_come_first_within_a_status() -> None:
    claims = classify_claims(
        [
            claim("Two sources", supported=["google-deepmind", "ollama"]),
            claim(
                "Three sources",
                supported=["google-deepmind", "ollama", "huggingface-blog"],
            ),
        ],
        SOURCES,
    )

    assert claims[0].text == "Three sources"


def test_counts_are_reported_by_status() -> None:
    claims = classify_claims(
        [
            claim("A", supported=["google-deepmind", "ollama"]),
            claim("B", supported=["ollama"]),
            claim("C", supported=["ollama"], contradicted=["the-verge-ai"]),
        ],
        SOURCES,
    )

    counts = summarise_claims(claims)

    assert counts["total"] == 3
    assert counts["verified"] == 1
    assert counts["unverified"] == 1
    assert counts["contradicted"] == 1


def test_no_claims_reports_zeroes_rather_than_an_empty_dict() -> None:
    counts = summarise_claims([])

    assert counts["total"] == 0
    assert counts["verified"] == 0
