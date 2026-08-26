"""Entity extraction.

Clustering needs to know what an article is *about* before it can decide that two
articles are about the same thing. Title similarity alone is not enough: "OpenAI releases
GPT-X" and "GPT-X is now available to developers" share almost no characters but are
obviously one event, while "Google launches Gemini 4" and "Google launches Willow 2"
share most of their characters and are two events.

The extractor is a lexicon plus a few patterns, deliberately, not a named-entity model:

* It is deterministic, so a cluster formed today re-forms identically tomorrow.
* The domain is narrow and its vocabulary is small and slow-moving — a few dozen labs and
  model families cover the overwhelming majority of AI news.
* A model would introduce a dependency, a download, and non-reproducible output, in
  exchange for recall on names that matter least.

Its known weakness is a new company nobody has heard of. That case falls back to title
similarity, and the lexicon is one edit away from covering it.
"""

from __future__ import annotations

import re
from functools import lru_cache

ORGANISATIONS: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "deepmind": "google-deepmind",
    "google deepmind": "google-deepmind",
    "meta": "meta",
    "microsoft": "microsoft",
    "nvidia": "nvidia",
    "mistral": "mistral",
    "hugging face": "huggingface",
    "huggingface": "huggingface",
    "xai": "xai",
    "apple": "apple",
    "amazon": "amazon",
    "aws": "amazon",
    "ibm": "ibm",
    "cohere": "cohere",
    "stability ai": "stability",
    "perplexity": "perplexity",
    "databricks": "databricks",
    "deepseek": "deepseek",
    "alibaba": "alibaba",
    "baidu": "baidu",
    "tencent": "tencent",
    "bytedance": "bytedance",
    "moonshot": "moonshot",
    "groq": "groq",
    "cerebras": "cerebras",
    "together ai": "together",
    "scale ai": "scale",
    "runway": "runway",
    "midjourney": "midjourney",
    "figure": "figure",
    "tesla": "tesla",
    "salesforce": "salesforce",
    "oracle": "oracle",
    "intel": "intel",
    "amd": "amd",
    "arm": "arm",
    "qualcomm": "qualcomm",
    "eu": "eu",
    "european union": "eu",
}
"""Organisation names mapped to a stable key, so "AWS" and "Amazon" are one entity."""

MODEL_FAMILIES: frozenset[str] = frozenset(
    {
        "gpt",
        "claude",
        "gemini",
        "gemma",
        "llama",
        "mistral",
        "mixtral",
        "qwen",
        "deepseek",
        "phi",
        "grok",
        "sora",
        "dall-e",
        "dalle",
        "whisper",
        "flux",
        "midjourney",
        "stable diffusion",
        "codestral",
        "command",
        "nova",
        "titan",
        "falcon",
        "yi",
        "kimi",
        "ernie",
        "olmo",
        "pythia",
        "bert",
        "clip",
        "sam",
        "veo",
        "imagen",
        "willow",
        "nemotron",
        "granite",
        "jamba",
        "smollm",
    }
)

PRODUCTS: dict[str, str] = {
    "copilot": "copilot",
    "chatgpt": "chatgpt",
    "cursor": "cursor",
    "pytorch": "pytorch",
    "tensorflow": "tensorflow",
    "jax": "jax",
    "cuda": "cuda",
    "vllm": "vllm",
    "ollama": "ollama",
    "langchain": "langchain",
    "transformers": "transformers",
    "kubernetes": "kubernetes",
}

_VERSIONED_MODEL = re.compile(
    r"\b(" + "|".join(sorted(MODEL_FAMILIES, key=len, reverse=True)) + r")[\s\-]?(\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)

_ACRONYM = re.compile(r"\b([A-Z]{2,6})\b")

_PROPER_NOUN = re.compile(r"\b([A-Z][a-zA-Z]{3,})\b")

_STOP_ACRONYMS: frozenset[str] = frozenset(
    {
        "THE",
        "AND",
        "FOR",
        "NEW",
        "NOW",
        "WITH",
        "HOW",
        "WHY",
        "ALL",
        "ONE",
        "TWO",
        "TOP",
        "CAN",
        "VIA",
        "IT",
        "US",
        "UK",
        "USA",
        # Ubiquitous in an AI feed, so they identify nothing.
        "AI",
        "ML",
        "LLM",
        "LLMS",
        "NLP",
        "RAG",
        "SOTA",
        "API",
        "APIS",
        "SDK",
        "GPU",
        "GPUS",
        "CPU",
        "DATA",
        "OPEN",
        "CEO",
        "CTO",
        "COO",
    }
)
"""Too common to distinguish one story from another. "AI" in an AI feed is noise, and so,
measured on live data, is "LLM": it merged sixteen unrelated arXiv papers into one
event."""


ENTITY_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("product:", 0.7),
    ("model:", 0.6),
    ("term:", 0.6),
    ("org:", 0.25),
)
"""How much an entity says about *which* story this is.

An organisation says almost nothing: OpenAI appears in a dozen unrelated stories on any
given day. A specific model version says almost everything. Measured on live data, treating
all entities alike merged twenty-two separate OpenAI articles into a single event.
"""

GATE_WEIGHT = 0.5
"""Minimum weight of a shared entity for a merge to be considered at all. Above the
weight of an organisation, so a shared company name is never sufficient by itself."""

DECISIVE_WEIGHT = 1.0
"""Weight at which a shared entity carries a merge on its own, without the wording having
to agree. Only a model family *and* version qualifies.

Nothing weaker survived contact with live data. An attempt to treat distinctive
capitalised words as decisive — "Cowork", "Nemotron", "SageMaker" — collapsed immediately:
a single shared word like "Desktop" merged a story about SpaceX shares with Anthropic's
Cowork launch, and multi-source events jumped from 8 to 88, nearly all of them wrong.

The consequence is deliberate and worth stating plainly: this stage is tuned for
precision, and it under-clusters. Two outlets describing one event in genuinely different
words are left as two events unless they name the same model version. Closing that gap
needs semantics rather than string overlap, which is what the LLM budget in P5 is for —
adjudicating a handful of borderline pairs is exactly the kind of judgement worth spending
a model call on, and exactly the kind of guess that must not be made by keyword rules.
"""


def weight(entity: str) -> float:
    """Specificity of one entity."""
    if entity.startswith("model:") and "-" in entity.removeprefix("model:"):
        # A family plus a version, e.g. "model:gemini-3.5". Far more specific than the
        # bare family, and the strongest signal available.
        return 1.0
    for prefix, value in ENTITY_WEIGHTS:
        if entity.startswith(prefix):
            return value
    return 0.4


def weighted_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Entity overlap, weighted by specificity. 0.0 to 1.0."""
    if not left or not right:
        return 0.0
    union = left | right
    total = sum(weight(entity) for entity in union)
    if not total:
        return 0.0
    shared = sum(weight(entity) for entity in left & right)
    return shared / total


def strongest_shared(left: frozenset[str], right: frozenset[str]) -> float:
    """Weight of the most specific entity the two sets have in common."""
    shared = left & right
    return max((weight(entity) for entity in shared), default=0.0)


@lru_cache(maxsize=4096)
def extract_entities(text: str) -> frozenset[str]:
    """Entities mentioned in a piece of text, as stable lower-case keys.

    A versioned model yields two entities — the family and the specific version — so that
    "Gemini 3.5" and "Gemini 4" share the family but differ on the version, which lets the
    clusterer treat them as related without merging them.
    """
    if not text:
        return frozenset()

    lowered = text.lower()
    found: set[str] = set()

    for name, key in ORGANISATIONS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            found.add(f"org:{key}")

    for name, key in PRODUCTS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            found.add(f"product:{key}")

    for family, version in _VERSIONED_MODEL.findall(text):
        found.add(f"model:{family.lower()}")
        found.add(f"model:{family.lower()}-{version}")

    for family in MODEL_FAMILIES:
        if re.search(rf"\b{re.escape(family)}\b", lowered):
            found.add(f"model:{family}")

    for acronym in _ACRONYM.findall(text):
        if acronym not in _STOP_ACRONYMS:
            found.add(f"term:{acronym.lower()}")

    return frozenset(found)


def article_entities(title: str, summary: str | None = None) -> frozenset[str]:
    """Entities for one article.

    The title carries the entities that define the story; the summary is included because
    a headline often omits the company name that the first sentence supplies.
    """
    entities = extract_entities(title)
    if summary:
        entities |= extract_entities(summary[:400])
    return entities


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Overlap of two entity sets, 0.0 to 1.0."""
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def has_conflicting_version(left: frozenset[str], right: frozenset[str]) -> bool:
    """True when both sets name the same model family at different versions.

    "Gemini 3.5" and "Gemini 4" are two announcements, however similar their headlines.
    A version present on one side and absent on the other is not a conflict — a write-up
    that omits the version number is still about the same release.
    """
    for family in _families(left) & _families(right):
        left_versions = _versions_of(left, family)
        right_versions = _versions_of(right, family)
        if left_versions and right_versions and not (left_versions & right_versions):
            return True
    return False


def _families(entities: frozenset[str]) -> set[str]:
    return {
        entity.removeprefix("model:")
        for entity in entities
        if entity.startswith("model:") and "-" not in entity.removeprefix("model:")
    }


def _versions_of(entities: frozenset[str], family: str) -> set[str]:
    prefix = f"model:{family}-"
    return {entity.removeprefix(prefix) for entity in entities if entity.startswith(prefix)}
