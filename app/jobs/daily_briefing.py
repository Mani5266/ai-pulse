"""Entrypoint for the daily pipeline run.

Invoked as ``python -m app.jobs.daily_briefing`` by the GitHub Actions cron and, during
development, by hand. Stages are added phase by phase; P1 implements ingestion and
persistence.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler

from app.briefing.builder import build_briefing
from app.briefing.models import BriefingStats
from app.briefing.render_telegram import render_telegram
from app.core.config import Settings, get_settings
from app.core.errors import ConfigError
from app.delivery.telegram import TelegramDelivery
from app.ingestion.dedup import deduplicate
from app.ingestion.normalize import enrich_all
from app.ingestion.recency import filter_recent
from app.ingestion.runner import ingest_all, summarise
from app.ingestion.sources import credibility_by_id, enabled_sources, load_sources
from app.intelligence.clustering import ClusterConfig, cluster_articles
from app.llm.analysis import analyse_stories, score_impact, verify_claims
from app.llm.analysis import summarise as summarise_analysis
from app.llm.provider import LLMError, LLMProvider, build_provider
from app.ranking.profile import load_profile
from app.ranking.scoring import score_events
from app.ranking.shortlist import build_shortlist
from app.storage.briefing_store import build_site, write_briefing
from app.storage.event_store import latest_events, write_events
from app.storage.ndjson_store import (
    append_articles,
    known_content_hashes,
    known_ids,
    read_articles,
    recent_days,
)
from app.storage.run_store import FeedOutcome, RunRecord, write_run
from app.storage.state import RunState, compute_window, read_state, write_state

logger = logging.getLogger("ai_pulse")


def configure_logging(settings: Settings) -> None:
    """Log to stdout, and to a file when one is configured.

    A scheduled run has nowhere to print, so without the file there is no record of what
    happened at 07:30 beyond a task exit code.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if settings.log_file is not None:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                settings.log_file,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )

    # httpx logs every request URL at INFO, and Telegram carries the bot token in the
    # path: https://api.telegram.org/bot<TOKEN>/sendMessage. At INFO that writes a live
    # credential into the log file on every delivery, and into any log the user shares.
    # Raised to WARNING so failures are still visible and successful URLs are not.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def run(settings: Settings) -> int:
    """Run one pipeline pass, recording what happened either way.

    A failed run leaves a record behind before the exit code propagates: nobody is
    watching at 02:00 UTC, and a failure that writes nothing is a failure nobody can
    diagnose later.
    """
    started_at = datetime.now(UTC)
    started = time.monotonic()

    try:
        return _run(settings, started_at, started)
    except Exception as exc:
        logger.exception("pipeline failed")
        write_run(
            settings.data_dir,
            RunRecord(
                started_at=started_at,
                finished_at=datetime.now(UTC),
                ok=False,
                duration_seconds=round(time.monotonic() - started, 1),
                error=f"{type(exc).__name__}: {exc}",
            ),
        )
        raise


def _run(settings: Settings, started_at: datetime, started: float) -> int:
    """The pipeline itself."""
    logger.info(
        "pipeline start provider=%s model=%s call_budget=%d data_dir=%s",
        settings.llm_provider,
        settings.llm_model,
        settings.llm_call_budget,
        settings.data_dir,
    )

    try:
        all_sources = load_sources()
        sources = enabled_sources(all_sources)
        profile = load_profile()
    except ConfigError as exc:
        logger.error("configuration unusable: %s", exc)
        write_run(
            settings.data_dir,
            RunRecord(
                started_at=started_at,
                finished_at=datetime.now(UTC),
                ok=False,
                duration_seconds=round(time.monotonic() - started, 1),
                error=f"configuration: {exc}",
            ),
        )
        return 2

    state = read_state(settings.data_dir)
    window = compute_window(
        state,
        first_run_days=settings.first_run_days,
        max_catchup_days=settings.max_catchup_days,
    )
    logger.info(
        "covering %s (%.1f hours%s)",
        window.start.strftime("%Y-%m-%d %H:%M UTC"),
        window.hours,
        ", first run" if window.is_first_run else ", clamped" if window.was_clamped else "",
    )

    logger.info("ingesting %d enabled sources", len(sources))
    results = ingest_all(sources, settings)
    stats = summarise(results)

    for result in results:
        if not result.ok:
            logger.warning("source failed: %s: %s", result.source_id, result.error)

    today = datetime.now(UTC).date()

    # Articles arrive in source-registry order, which puts first-party announcements
    # before news write-ups of them. Deduplication keeps the first copy it sees, so that
    # ordering decides which copy survives.
    # A feed hands over its whole current window, which for a quiet blog is a year. Only
    # what falls inside this run's window is news; the rest was fetched so that clustering
    # can still recognise a story it has seen before.
    recency = filter_recent([article for result in results for article in result.articles], window)
    articles = enrich_all(recency.fresh)

    memory = recent_days(today, settings.dedup_memory_days)
    deduped = deduplicate(
        articles,
        known_ids=known_ids(settings.data_dir, memory),
        known_content_hashes=known_content_hashes(settings.data_dir, memory),
        title_threshold=settings.dedup_title_threshold,
    )

    written = append_articles(settings.data_dir, today, deduped.unique)

    logger.info(
        "ingestion complete sources=%d ok=%d failed=%d articles=%d in-window=%d stale=%d",
        stats["sources"],
        stats["ok"],
        stats["failed"],
        stats["articles"],
        len(recency.fresh),
        len(recency.stale),
    )
    logger.info(
        "deduplication complete unique=%d removed=%d rate=%.1f%% stored=%d (%s)",
        len(deduped.unique),
        len(deduped.duplicates),
        deduped.duplicate_rate * 100,
        written,
        ", ".join(f"{reason}={count}" for reason, count in deduped.counts_by_reason().items())
        or "no duplicates",
    )

    event_window = list(reversed(recent_days(today, settings.event_memory_days)))
    clustered = cluster_articles(
        deduped.unique,
        existing=latest_events(settings.data_dir, event_window),
        config=ClusterConfig(threshold=settings.cluster_threshold),
    )
    logger.info(
        "clustering complete events=%d new=%d updated=%d multi_source=%d ratio=%.2f",
        len(clustered.events),
        len(clustered.new_event_ids),
        len(clustered.updated_event_ids),
        len(clustered.multi_source_events),
        clustered.stats()["articles_per_event"],
    )

    # Novelty is measured against what the briefing already covered, so the history must
    # exclude the events this run just touched.
    touched_ids = {event.id for event in clustered.events}
    seen_entities = {
        entity
        for event in latest_events(settings.data_dir, event_window)
        if event.id not in touched_ids
        for entity in event.entities
    }

    # Two different questions, and conflating them was a bug: ingestion asks what has
    # not been seen yet, the briefing asks what the reader should know now. A re-run
    # minutes after the last one ingests nothing new, but must still report the day.
    briefing_cutoff = datetime.now(UTC) - timedelta(hours=settings.briefing_lookback_hours)
    touched = {event.id: event for event in clustered.events}
    candidates = list(clustered.events) + [
        event
        for event in latest_events(settings.data_dir, event_window)
        if event.id not in touched and event.last_updated >= briefing_cutoff
    ]
    logger.info(
        "ranking %d events (%d new this run, %d carried from the last %dh)",
        len(candidates),
        len(clustered.events),
        len(candidates) - len(clustered.events),
        settings.briefing_lookback_hours,
    )

    scored = score_events(
        candidates,
        profile=profile,
        source_credibility=credibility_by_id(all_sources),
        today=today,
        seen_entities=seen_entities,
    )
    shortlist = build_shortlist(
        scored,
        limit=settings.max_events_to_llm,
        max_per_category=settings.max_events_per_category,
    )

    provider = _build_provider(settings)
    if provider is None:
        write_events(settings.data_dir, today, [item.with_score() for item in scored])
        logger.warning("no model available; publishing the deterministic ranking only")
        return 0

    stored_articles = {
        article.id: article for article in read_articles(settings.data_dir, today) if article.id
    }
    # Two calls per story now follow scoring — a summary and a claim extraction — and
    # each may retry once, so the reservation is four per story.
    analysed = score_impact(
        shortlist.selected,
        stored_articles,
        provider,
        reserve=settings.stories_per_briefing * 4,
    )
    analysed = analyse_stories(
        analysed, stored_articles, provider, limit=settings.stories_per_briefing
    )
    analysed = verify_claims(
        analysed, stored_articles, provider, limit=settings.stories_per_briefing
    )

    analysed_ids = {item.event.id for item in analysed}
    write_events(
        settings.data_dir,
        today,
        [item.with_score() for item in analysed]
        + [item.with_score() for item in scored if item.event.id not in analysed_ids],
    )

    stats_line = shortlist.stats()
    logger.info(
        "ranking complete considered=%d shortlisted=%d top=%.2f cutoff=%.2f (%s)",
        stats_line["considered"],
        stats_line["selected"],
        stats_line["top_score"],
        stats_line["cut_off_score"],
        ", ".join(f"{name}={count}" for name, count in sorted(shortlist.categories().items())),
    )
    for item in shortlist.selected[:5]:
        logger.info(
            "  %.2f [%s] %s (%d sources)",
            item.score,
            item.event.category.value,
            item.event.canonical_title[:70],
            item.event.source_count,
        )

    analysis_stats = summarise_analysis(analysed)
    logger.info(
        "analysis complete provider=%s scored=%d degraded=%d summarised=%d "
        "claims=%d corroborated=%d calls=%s",
        provider.name,
        analysis_stats["model_scored"],
        analysis_stats["degraded"],
        analysis_stats["summarised"],
        analysis_stats["claims"],
        analysis_stats["corroborated_claims"],
        provider.stats.as_dict(),
    )
    for story in analysed[: settings.stories_per_briefing]:
        headline = story.analysis.headline if story.analysis else story.event.canonical_title
        logger.info("  %.2f %s", story.final_score, headline[:80])

    briefing = build_briefing(
        analysed,
        stored_articles,
        day=today,
        limit=settings.stories_per_briefing,
        covers_since=window.start,
        stats=BriefingStats(
            feeds_ok=int(stats["ok"]),
            feeds_failed=int(stats["failed"]),
            articles=int(stats["articles"]),
            duplicates_removed=len(deduped.duplicates),
            events=len(clustered.events),
            events_shortlisted=len(shortlist.selected),
            model_calls=provider.stats.attempted,
            model_failures=provider.stats.failed,
            provider=provider.name,
            runtime_seconds=round(time.monotonic() - started, 1),
        ),
    )

    # Persist and publish before delivering. Delivery is the one stage that depends on
    # somebody else's server, so a failure there must cost nothing.
    written_path = write_briefing(settings.data_dir, briefing)
    if written_path is None:
        logger.warning("this run produced nothing; the previous briefing stands")
    build_site(settings.data_dir, settings.site_dir)

    # Advanced only now, and only to the window this run actually covered. A crash before
    # this point means the next run re-covers the same span rather than skipping it.
    write_state(
        settings.data_dir,
        RunState(
            last_briefing_at=window.end,
            last_run_at=datetime.now(UTC),
            successful_runs=state.successful_runs + 1,
            total_runs=state.total_runs + 1,
        ),
    )

    delivery = TelegramDelivery(settings)
    try:
        delivered = delivery.send(render_telegram(briefing))
    finally:
        delivery.close()

    if delivered.failed:
        logger.warning(
            "delivery failed: %s (the briefing is saved and will retry)", delivered.detail
        )
    write_run(
        settings.data_dir,
        RunRecord(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            ok=True,
            duration_seconds=briefing.stats.runtime_seconds,
            window_start=window.start,
            window_hours=round(window.hours, 2),
            first_run=window.is_first_run,
            window_clamped=window.was_clamped,
            feeds=[
                FeedOutcome(
                    source_id=result.source_id,
                    ok=result.ok,
                    articles=result.article_count,
                    error=result.error,
                    duration_seconds=result.duration_seconds,
                )
                for result in results
            ],
            articles_fetched=int(stats["articles"]),
            articles_in_window=len(recency.fresh),
            articles_stored=written,
            duplicates_removed=len(deduped.duplicates),
            events_touched=len(clustered.events),
            events_multi_source=len(clustered.multi_source_events),
            events_shortlisted=len(shortlist.selected),
            stories_published=len(briefing.stories),
            provider=provider.name,
            model_calls=provider.stats.attempted,
            model_failures=provider.stats.failed,
            model_rate_limited=provider.stats.rate_limited,
            schema_violations=provider.stats.schema_violations,
            claims_extracted=int(analysis_stats["claims"]),
            claims_corroborated=int(analysis_stats["corroborated_claims"]),
            delivered=delivered.ok,
            delivery_error=delivered.detail or None,
        ),
    )

    logger.info(
        "briefing complete stories=%d delivered=%s runtime=%.1fs",
        len(briefing.stories),
        delivered.ok,
        briefing.stats.runtime_seconds,
    )

    if stats["ok"] == 0:
        logger.error("every source failed; nothing to work with")
        return 1

    return 0


def _build_provider(settings: Settings) -> LLMProvider | None:
    """Construct the configured provider, or None if it cannot run.

    A missing API key is a configuration gap, not a crash: the run still produces a
    deterministically ranked set of events, which is a degraded briefing rather than none.
    """
    try:
        return build_provider(settings)
    except LLMError as exc:
        logger.warning("model unavailable: %s", exc)
        return None


def main() -> int:
    """Console-script entrypoint."""
    settings = get_settings()
    configure_logging(settings)
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
