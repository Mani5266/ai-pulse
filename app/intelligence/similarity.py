"""Title similarity.

Character trigrams with the Dice coefficient. No embeddings, no model, no vector
database — and that is a deliberate choice, not a shortcut:

* It is deterministic, so a test written today still passes next year.
* It is fast enough that all-pairs comparison over a day's articles is milliseconds.
* It degrades sensibly on the failure mode that matters here, which is a publisher
  rewording a headline slightly ("OpenAI releases GPT-X" against "OpenAI has released
  GPT-X"), rather than restating it entirely.

Character trigrams rather than word tokens because publishers change word forms —
"releases" against "released", "1M" against "1 million" — while leaving most character
sequences intact.

Where this is *not* enough is genuinely different headlines about the same event, and
that is what event clustering in P3 exists to handle.
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.ingestion.hashing import normalize_text

TRIGRAM_SIZE = 3

_NUMBER = re.compile(r"\d+(?:[.\-]\d+)*")


@lru_cache(maxsize=4096)
def trigrams(text: str) -> frozenset[str]:
    """Character trigrams of normalised text.

    The text is padded so that short titles still produce a usable set, and cached
    because deduplication compares each title against many others.
    """
    normalized = normalize_text(text)
    if not normalized:
        return frozenset()

    padded = f"  {normalized} "
    return frozenset(
        padded[index : index + TRIGRAM_SIZE] for index in range(len(padded) - TRIGRAM_SIZE + 1)
    )


def dice(left: frozenset[str], right: frozenset[str]) -> float:
    """Dice coefficient: ``2 * |A n B| / (|A| + |B|)``, in the range 0.0 to 1.0.

    Dice rather than Jaccard because it is more forgiving of one title being longer than
    the other, which is the common case when a publisher appends a subtitle.
    """
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    if not overlap:
        return 0.0
    return 2.0 * overlap / (len(left) + len(right))


def title_similarity(left: str, right: str) -> float:
    """Similarity of two titles, 0.0 to 1.0."""
    return dice(trigrams(left), trigrams(right))


def is_near_duplicate(left: str, right: str, threshold: float) -> bool:
    """True when two titles are similar enough to be the same article.

    ``threshold`` is deliberately the caller's decision: deduplication uses a high bar,
    while clustering in P3 will use a lower one.
    """
    return title_similarity(left, right) >= threshold


MONTHS: frozenset[str] = frozenset(
    {
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)


@lru_cache(maxsize=4096)
def identity_signature(text: str) -> tuple[str, ...]:
    """The tokens in a title that carry identity rather than phrasing.

    Numbers and month names. Trigram similarity is nearly blind to both, and in AI news
    both are frequently the entire difference between two items. Measured on one real day
    of 22 feeds, every title pair scoring above 0.85 differed *only* in these tokens::

        0.91  "The latest AI news we announced in July 2026"
              "The latest AI news we announced in June 2026"
        0.90  "sqlite-utils 4.2.1"
              "sqlite-utils 4.2"
        0.83  "Introducing Gemini 3.6 Flash, 3.5 Flash-Lite, and 3.5 Flash"
              "Introducing Gemini 3.5 Flash Cyber"

    Those are two different monthly roundups, two different releases and two different
    model announcements. Every one would have been wrongly merged on similarity alone,
    and the roundups differ by a single word — "June" against "July" — which is three
    characters out of forty.
    """
    # Numbers come from the raw text: normalisation strips punctuation, which would turn
    # "4.2.1" into "421" and lose the distinction from "4.21".
    numbers = _NUMBER.findall(text)
    months = [word for word in normalize_text(text).split() if word in MONTHS]
    return tuple(numbers) + tuple(months)


def signatures_agree(left: str, right: str) -> bool:
    """True when two titles carry the same identity tokens.

    A merge is only safe when neither version number, model number, date nor month
    differs between the two.
    """
    return identity_signature(left) == identity_signature(right)
