"""Event clustering: articles in, events out.

This is the stage the whole product rests on. Deduplication removes copies of one
*article*; clustering groups different articles about one *development*. Four outlets
covering a model release become one event with four sources — which is both what the
briefing should say and, since independent coverage is a signal of importance, an input
to ranking.

**How two articles are matched.** A blend of entity overlap and title similarity::

    score = 0.55 * entity_overlap + 0.45 * title_similarity

Neither signal is sufficient alone:

* Titles alone fail on "OpenAI releases GPT-X" against "GPT-X is now available to
  developers" — one event, almost no shared characters.
* Entities alone fail on two separate Google announcements in one day, which share the
  organisation and nothing else.

Entities are weighted by how much they narrow down *which* story this is: a specific model
version is decisive, a bare model family is suggestive, an organisation is nearly
worthless. Measured on live data, treating them alike merged twenty-two separate OpenAI
articles into one event.

A pair must also clear a gate — a shared entity specific enough to matter, or a title
similarity high enough to stand alone — so that a weak score cannot accumulate from two
weak halves.

**Version conflicts block a merge.** "Gemini 3.5" and "Gemini 4" are two announcements
however similar their wording, so a model family named at different versions on each side
prevents clustering. A version on one side and none on the other is not a conflict: a
write-up that omits the number is still about the same release.

**Cross-day.** Existing events from recent days take part in matching, so an article
published today can attach to Monday's event and move its ``last_updated``. That is what
makes AI-Pulse a timeline rather than a daily newsletter, and it is why the algorithm is
single-pass and order-dependent rather than a global optimisation: yesterday's clustering
must not be re-decided today.

**Cost.** Each article is compared against existing clusters, capped at the most recent
members of each. At ~500 articles and a few hundred live events, that is comfortably
under a second, with no model involved.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.models import Article, Event
from app.intelligence.categories import Category, classify
from app.intelligence.entities import (
    DECISIVE_WEIGHT,
    GATE_WEIGHT,
    article_entities,
    has_conflicting_version,
    strongest_shared,
    weighted_overlap,
)
from app.intelligence.similarity import title_similarity

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    """Tuning knobs, all deterministic and all testable."""

    threshold: float = 0.45
    """Blended score above which an article joins an event."""

    entity_weight: float = 0.55
    """Entity overlap counts slightly more than wording, because wording varies most."""

    gate_title_similarity: float = 0.60
    """A pair with no shared entity may still match on wording alone above this."""

    min_title_similarity: float = 0.40
    """Wording must corroborate the entities. Without this floor, two articles sharing
    only "ChatGPT" scored high enough to merge on the entity alone — fifteen unrelated
    OpenAI articles in one event on live data."""

    same_source_title_similarity: float = 0.60
    """Two articles from the *same* publisher, on the same day, are almost always two
    different stories: a publisher does not report its own news twice. Merging them
    therefore needs near-duplicate wording rather than a shared entity. On live data this
    single rule dissolved the worst clusters — nine separate openai.com posts that shared
    nothing but the word "ChatGPT", and six NVIDIA posts that shared only "NVIDIA"."""

    decisive_entity_weight: float = DECISIVE_WEIGHT
    """A shared entity this specific — a distinctive product name, or a model family
    *and* version — carries a merge without the wording having to agree."""

    max_titles_compared: int = 12
    """Per event, how many member titles to compare against. Bounds the cost."""


@dataclass
class _Cluster:
    """Mutable working state for one event during a run."""

    id: str
    canonical_title: str
    category: Category
    entities: set[str]
    titles: list[str]
    article_ids: list[str]
    source_ids: list[str]
    first_seen: datetime
    last_updated: datetime
    is_new: bool
    touched: bool = False

    def to_event(self) -> Event:
        return Event(
            id=self.id,
            canonical_title=self.canonical_title,
            category=self.category,
            entities=sorted(self.entities),
            article_ids=list(self.article_ids),
            source_ids=list(self.source_ids),
            first_seen=self.first_seen,
            last_updated=self.last_updated,
        )


@dataclass
class ClusterResult:
    """Events touched by this run, plus the counts for the run log."""

    events: list[Event] = field(default_factory=list)
    new_event_ids: set[str] = field(default_factory=set)
    updated_event_ids: set[str] = field(default_factory=set)
    articles_clustered: int = 0

    @property
    def multi_source_events(self) -> list[Event]:
        """Events corroborated by more than one source."""
        return [event for event in self.events if event.source_count > 1]

    def stats(self) -> dict[str, int | float]:
        return {
            "articles": self.articles_clustered,
            "events_touched": len(self.events),
            "events_new": len(self.new_event_ids),
            "events_updated": len(self.updated_event_ids),
            "multi_source_events": len(self.multi_source_events),
            "articles_per_event": (
                round(self.articles_clustered / len(self.events), 2) if self.events else 0.0
            ),
        }


def _article_time(article: Article) -> datetime:
    return article.published_at or article.fetched_at


def _score(
    article: Article,
    article_entity_set: frozenset[str],
    cluster: _Cluster,
    config: ClusterConfig,
) -> float:
    """Blended similarity of one article to one cluster, or 0.0 if a gate fails."""
    if has_conflicting_version(article_entity_set, frozenset(cluster.entities)):
        return 0.0

    article_title = article.title

    cluster_entities = frozenset(cluster.entities)
    entity_overlap = weighted_overlap(article_entity_set, cluster_entities)
    best_title = max(
        (
            title_similarity(article_title, title)
            for title in cluster.titles[-config.max_titles_compared :]
        ),
        default=0.0,
    )

    shared_weight = strongest_shared(article_entity_set, cluster_entities)

    # A shared organisation is not enough on its own: OpenAI appears in a dozen unrelated
    # stories a day. Either the shared entity is specific, or the wording carries it.
    if shared_weight < GATE_WEIGHT and best_title < config.gate_title_similarity:
        return 0.0

    # And a shared *family* is not enough either. "Gemini Robotics 2" and "Gemini 3.6
    # Flash" are both Gemini and both Google, and are two announcements. Only a shared
    # model version is decisive on its own; everything else needs the wording to agree.
    if shared_weight < config.decisive_entity_weight and best_title < config.min_title_similarity:
        return 0.0

    # A publisher does not report its own news twice, so an article joining a cluster that
    # already contains its own source needs near-duplicate wording.
    if article.source_id in cluster.source_ids and best_title < config.same_source_title_similarity:
        return 0.0

    blended = config.entity_weight * entity_overlap + (1.0 - config.entity_weight) * best_title

    # Wording alone, when it is strong enough, should not be capped by the blend: two
    # near-identical headlines naming no known entity are still one story.
    if best_title >= config.gate_title_similarity:
        return max(blended, best_title)

    # Nor should a decisive shared entity be outvoted by differing wording. Two articles
    # that both name "Gemma 4" are about Gemma 4, however differently they are phrased.
    # The floor is the threshold itself, so a better-matching cluster still wins.
    if shared_weight >= config.decisive_entity_weight:
        return max(blended, config.threshold)

    return blended


def cluster_articles(
    articles: Sequence[Article],
    *,
    existing: Sequence[Event] = (),
    config: ClusterConfig | None = None,
) -> ClusterResult:
    """Group articles into events, extending recent events where they belong.

    Articles must already carry an ``id`` from :func:`app.ingestion.normalize.enrich`.
    Input order decides which article seeds an event; callers pass registry order, so a
    first-party announcement titles the event rather than a write-up of it.
    """
    settings = config or ClusterConfig()

    clusters: list[_Cluster] = [_from_event(event) for event in existing]
    result = ClusterResult()

    for article in articles:
        entities = article_entities(article.title, article.summary)
        best: _Cluster | None = None
        best_score = settings.threshold

        for cluster in clusters:
            score = _score(article, entities, cluster, settings)
            if score >= best_score:
                best, best_score = cluster, score

        if best is None:
            clusters.append(_seed(article, entities))
        else:
            _attach(best, article, entities)

        result.articles_clustered += 1

    for cluster in clusters:
        if not cluster.touched:
            continue
        result.events.append(cluster.to_event())
        if cluster.is_new:
            result.new_event_ids.add(cluster.id)
        else:
            result.updated_event_ids.add(cluster.id)

    logger.info(
        "clustering: %d articles into %d events (%d new, %d updated)",
        result.articles_clustered,
        len(result.events),
        len(result.new_event_ids),
        len(result.updated_event_ids),
    )
    return result


def _seed(article: Article, entities: frozenset[str]) -> _Cluster:
    """Start a new event from an article.

    The event id is derived from the seeding article's id, so it is stable across runs
    and traceable back to the article that opened the story.
    """
    when = _article_time(article)
    return _Cluster(
        id=f"evt_{article.id or ''}",
        canonical_title=article.title,
        category=classify(article.title, article.summary, source_id=article.source_id),
        entities=set(entities),
        titles=[article.title],
        article_ids=[article.id or str(article.url)],
        source_ids=[article.source_id],
        first_seen=when,
        last_updated=when,
        is_new=True,
        touched=True,
    )


def _attach(cluster: _Cluster, article: Article, entities: frozenset[str]) -> None:
    """Add an article to an existing cluster."""
    article_key = article.id or str(article.url)
    if article_key in cluster.article_ids:
        return

    cluster.article_ids.append(article_key)
    cluster.titles.append(article.title)
    cluster.entities |= entities
    if article.source_id not in cluster.source_ids:
        cluster.source_ids.append(article.source_id)

    when = _article_time(article)
    cluster.first_seen = min(cluster.first_seen, when)
    cluster.last_updated = max(cluster.last_updated, when)
    cluster.touched = True


def _from_event(event: Event) -> _Cluster:
    """Rebuild working state from a stored event.

    Only the canonical title survives storage, not every member title, which is a
    deliberate trade: storing every title would grow the committed record for a gain that
    matters only on the day an event is formed.
    """
    return _Cluster(
        id=event.id,
        canonical_title=event.canonical_title,
        category=event.category,
        entities=set(event.entities),
        titles=[event.canonical_title],
        article_ids=list(event.article_ids),
        source_ids=list(event.source_ids),
        first_seen=event.first_seen,
        last_updated=event.last_updated,
        is_new=False,
    )
