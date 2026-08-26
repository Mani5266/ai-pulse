"""Article deduplication.

Three passes, cheapest first, because each one removes work from the next:

1. **Canonical URL** — string equality on the id derived from the canonical URL. Catches
   tracking-parameter variants, ``www`` variants, trailing slashes, AMP URLs and the same
   feed being re-read on a later run.
2. **Content hash** — equality on normalised title and summary. Catches syndicated copies
   published under different URLs.
3. **Title similarity** — character trigrams above a high threshold, guarded by a check
   that both titles carry the same numbers and months. Catches a publisher lightly
   rewording its own headline, without merging "sqlite-utils 4.2" into
   "sqlite-utils 4.2.1", or June's roundup into July's. See
   :func:`app.intelligence.similarity.identity_signature` for why that guard exists.

Nothing here uses a model, and that is the point: deduplication is the stage that decides
how much work every later stage does, so it must be deterministic, testable and fast.

**Which copy survives.** The first occurrence in input order wins. Callers pass articles
in source-registry order, and the registry is ordered primary, research, journalism,
ecosystem — so a first-party announcement is kept over a news write-up of it, which is
exactly the preference the product wants. Passing articles in a different order changes
which copy is kept, but never how many survive.

**Cost.** Passes 1 and 2 are hash lookups. Pass 3 compares each article against the
survivors so far, which is quadratic in the worst case; at ~500 articles a day that is a
few hundred thousand cached trigram comparisons and takes well under a second. A length
guard skips pairs whose titles are too different in size to reach the threshold.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.core.models import Article
from app.intelligence.similarity import signatures_agree, title_similarity, trigrams

logger = logging.getLogger(__name__)

DEFAULT_TITLE_THRESHOLD = 0.90
"""Deliberately high. Anything looser belongs to event clustering in P3, which groups
different headlines about one event rather than deleting them."""


@dataclass(frozen=True, slots=True)
class Duplicate:
    """One dropped article and the reason it was dropped."""

    article_id: str
    url: str
    source_id: str
    kept_id: str
    reason: str


@dataclass(slots=True)
class DedupResult:
    """Survivors, casualties, and the counts that go into the run log."""

    unique: list[Article] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)

    @property
    def input_count(self) -> int:
        return len(self.unique) + len(self.duplicates)

    @property
    def duplicate_rate(self) -> float:
        """Share of the input that was redundant. Reported in the run statistics."""
        if not self.input_count:
            return 0.0
        return len(self.duplicates) / self.input_count

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for duplicate in self.duplicates:
            counts[duplicate.reason] = counts.get(duplicate.reason, 0) + 1
        return counts

    def stats(self) -> dict[str, int | float]:
        stats: dict[str, int | float] = {
            "input": self.input_count,
            "unique": len(self.unique),
            "duplicates": len(self.duplicates),
            "duplicate_rate": round(self.duplicate_rate, 3),
        }
        stats.update(self.counts_by_reason())
        return stats


def _title_length_allows(left: str, right: str, threshold: float) -> bool:
    """Cheap rejection before computing similarity.

    Dice cannot exceed ``2 * min / (min + max)`` for sets of these sizes, so a pair whose
    titles differ greatly in length can be skipped without comparing them.
    """
    shorter, longer = sorted((len(left), len(right)))
    if longer == 0:
        return False
    return 2.0 * shorter / (shorter + longer) >= threshold


def deduplicate(
    articles: Sequence[Article],
    *,
    known_ids: Iterable[str] = (),
    known_content_hashes: Iterable[str] = (),
    title_threshold: float = DEFAULT_TITLE_THRESHOLD,
) -> DedupResult:
    """Remove duplicate articles from one batch.

    ``known_ids`` and ``known_content_hashes`` carry what previous days already stored, so
    an article that a feed still lists is not treated as news again. Articles must have
    been through :func:`app.ingestion.normalize.enrich` first.
    """
    result = DedupResult()

    seen_ids: dict[str, str] = dict.fromkeys(known_ids, "")
    seen_hashes: dict[str, str] = dict.fromkeys(known_content_hashes, "")
    kept_titles: list[tuple[str, str]] = []  # (article_id, title)

    for article in articles:
        identity = article.id or str(article.url)

        if identity in seen_ids:
            result.duplicates.append(_dropped(article, seen_ids[identity], "duplicate_url"))
            continue

        if article.content_hash and article.content_hash in seen_hashes:
            result.duplicates.append(
                _dropped(article, seen_hashes[article.content_hash], "duplicate_content")
            )
            continue

        near = _find_near_duplicate(article.title, kept_titles, title_threshold)
        if near is not None:
            result.duplicates.append(_dropped(article, near, "similar_title"))
            continue

        seen_ids[identity] = identity
        if article.content_hash:
            seen_hashes[article.content_hash] = identity
        kept_titles.append((identity, article.title))
        result.unique.append(article)

    if result.duplicates:
        logger.info(
            "deduplication: %d of %d removed (%s)",
            len(result.duplicates),
            result.input_count,
            ", ".join(f"{reason}={count}" for reason, count in result.counts_by_reason().items()),
        )

    return result


def _find_near_duplicate(
    title: str,
    kept: Sequence[tuple[str, str]],
    threshold: float,
) -> str | None:
    """Return the id of an already-kept article with a near-identical title."""
    if not trigrams(title):
        return None

    for kept_id, kept_title in kept:
        if not _title_length_allows(title, kept_title, threshold):
            continue
        # A differing version number, model number, date or month means a different
        # item, no matter how similar the surrounding words are.
        if not signatures_agree(title, kept_title):
            continue
        if title_similarity(title, kept_title) >= threshold:
            return kept_id
    return None


def _dropped(article: Article, kept_id: str, reason: str) -> Duplicate:
    return Duplicate(
        article_id=article.id or "",
        url=str(article.url),
        source_id=article.source_id,
        kept_id=kept_id,
        reason=reason,
    )
