"""Event categorisation.

Keyword rules, not a model. Two reasons: the categories are coarse enough that keywords
reach acceptable accuracy, and spending an LLM call on a label the ranking formula treats
as a coarse bucket would burn budget that the impact scores need.

Accuracy is measured in P9. If keywords prove too weak there, this module is the single
place a model would replace, and its interface would not change.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache

from app.ingestion.hashing import normalize_text


class Category(StrEnum):
    """What kind of development an event is."""

    MODEL_RELEASE = "model_release"
    RESEARCH = "research"
    OPEN_SOURCE = "open_source"
    DEVELOPER_TOOLS = "developer_tools"
    AI_AGENTS = "ai_agents"
    INFRASTRUCTURE = "infrastructure"
    FUNDING = "funding"
    ACQUISITION = "acquisition"
    POLICY = "policy"
    SAFETY = "safety"
    BENCHMARK = "benchmark"
    SECURITY = "security"
    PRODUCT = "product"
    OTHER = "other"


# Ordered most specific first: an acquisition that mentions funding is an acquisition,
# and a security disclosure about an agent framework is a security story.
CATEGORY_KEYWORDS: tuple[tuple[Category, tuple[str, ...]], ...] = (
    (
        Category.ACQUISITION,
        ("acquires", "acquisition", "acquired", "buys", "merger", "takeover"),
    ),
    (
        Category.FUNDING,
        (
            "raises",
            "funding round",
            "series a",
            "series b",
            "series c",
            "series d",
            "valuation",
            "seed round",
            "ipo",
            "investment",
        ),
    ),
    (
        Category.SECURITY,
        (
            "vulnerability",
            "exploit",
            "cve",
            "breach",
            "malware",
            "prompt injection",
            "jailbreak",
            "attack",
            "data leak",
        ),
    ),
    (
        Category.POLICY,
        (
            "regulation",
            "regulator",
            "lawsuit",
            "court",
            "copyright",
            "ban",
            "legislation",
            "act",
            "compliance",
            "antitrust",
            "executive order",
        ),
    ),
    (
        Category.SAFETY,
        (
            "alignment",
            "safety",
            "red team",
            "misuse",
            "guardrail",
            "responsible ai",
            "interpretability",
            "evaluation of risk",
        ),
    ),
    (
        Category.BENCHMARK,
        ("benchmark", "leaderboard", "state of the art", "sota", "eval suite", "scores"),
    ),
    (
        Category.MODEL_RELEASE,
        (
            "introducing",
            "releases",
            "release",
            "launches",
            "launch",
            "announcing",
            "now available",
            "unveils",
            "general availability",
            "preview",
        ),
    ),
    (
        Category.OPEN_SOURCE,
        ("open source", "open sources", "open weights", "apache 2", "mit license", "weights"),
    ),
    (
        Category.AI_AGENTS,
        ("agent", "agents", "agentic", "tool use", "autonomous", "multi agent"),
    ),
    (
        Category.DEVELOPER_TOOLS,
        ("sdk", "api", "cli", "ide", "developer", "library", "framework", "plugin", "extension"),
    ),
    (
        Category.INFRASTRUCTURE,
        (
            "gpu",
            "data center",
            "datacenter",
            "cluster",
            "inference",
            "training run",
            "chip",
            "accelerator",
            "compute",
            "serving",
        ),
    ),
    (
        Category.RESEARCH,
        ("paper", "arxiv", "we propose", "we present", "study", "findings", "preprint"),
    ),
    (
        Category.PRODUCT,
        ("feature", "update", "rollout", "users can", "app", "pricing", "subscription"),
    ),
)


@lru_cache(maxsize=512)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Word-bounded matcher for one keyword.

    Substring matching is wrong here and was a real defect: the policy keyword "act"
    matched inside "impact", "action" and "practice", which labelled a Gemini
    announcement as policy news.
    """
    return re.compile(r"\b" + re.escape(keyword) + r"\b")


def classify(title: str, summary: str | None = None, *, source_id: str = "") -> Category:
    """Categorise an event from its title and summary.

    arXiv sources are research by construction, so the source short-circuits the keyword
    rules: a paper announcing a model is still a paper.
    """
    if source_id.startswith("arxiv"):
        return Category.RESEARCH

    haystack = normalize_text(f"{title} {summary or ''}")
    if not haystack:
        return Category.OTHER

    for category, keywords in CATEGORY_KEYWORDS:
        if any(_keyword_pattern(keyword).search(haystack) for keyword in keywords):
            return category

    return Category.OTHER
