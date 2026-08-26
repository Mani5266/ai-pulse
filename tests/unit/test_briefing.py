"""Briefing construction and rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.briefing.builder import build_briefing
from app.briefing.models import Briefing, BriefingStats, Source, Story
from app.briefing.render_html import render_html, render_index
from app.briefing.render_telegram import (
    BODY_BUDGET,
    TELEGRAM_MAX_CHARS,
    render_telegram,
)
from app.core.models import Article, Event
from app.intelligence.categories import Category
from app.llm.analysis import AnalysedEvent
from app.llm.schemas import ImpactScores, StoryAnalysis
from app.ranking.scoring import ScoredEvent, Scores

DAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
MONDAY = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def article(article_id: str, source: str) -> Article:
    return Article(
        url=f"https://{source}.example.com/{article_id}",  # type: ignore[arg-type]
        title=f"Article {article_id}",
        source_id=source,
        fetched_at=NOW,
        id=article_id,
    )


def analysed(
    event_id: str = "evt_1",
    *,
    headline: str = "Gemma 4 released with open weights",
    article_ids: list[str] | None = None,
    sources: list[str] | None = None,
    with_analysis: bool = True,
    first_seen: datetime = NOW,
) -> AnalysedEvent:
    event = Event(
        id=event_id,
        canonical_title="Gemma 4 arrives",
        category=Category.MODEL_RELEASE,
        entities=["model:gemma-4"],
        article_ids=article_ids or ["a1"],
        source_ids=sources or ["google-deepmind"],
        first_seen=first_seen,
        last_updated=NOW,
    )
    analysis = (
        StoryAnalysis(
            headline=headline,
            what_happened="Google published Gemma 4 under a permissive licence.",
            why_it_matters="It is the strongest openly licensed model at this size.",
            developer_impact="Runs on a single consumer GPU.",
            confidence=0.9,
        )
        if with_analysis
        else None
    )
    return AnalysedEvent(
        scored=ScoredEvent(
            event=event,
            scores=Scores(credibility=7.0, novelty=7.0, personal_relevance=8.0),
        ),
        impact=ImpactScores(
            technical_impact=7.0,
            industry_impact=6.0,
            developer_impact=8.0,
            reasoning="Capable open model.",
        ),
        analysis=analysis,
    )


ARTICLES = {
    "a1": article("a1", "google-deepmind"),
    "a2": article("a2", "the-verge-ai"),
    "a3": article("a3", "google-deepmind"),
}


# --- building -----------------------------------------------------------------


def test_stories_are_built_from_analysed_events() -> None:
    briefing = build_briefing([analysed()], ARTICLES, day=DAY, limit=5)

    assert len(briefing.stories) == 1
    story = briefing.stories[0]
    assert story.headline == "Gemma 4 released with open weights"
    assert story.sources[0].source_id == "google-deepmind"


def test_an_event_without_a_summary_is_dropped_not_padded() -> None:
    """The failure the product exists to avoid: degrading into a feed reader while still
    looking like a briefing."""
    briefing = build_briefing(
        [analysed("evt_1", with_analysis=False), analysed("evt_2")],
        ARTICLES,
        day=DAY,
        limit=5,
    )

    assert len(briefing.stories) == 1
    assert briefing.stories[0].event_id == "evt_2"


def test_the_limit_is_respected() -> None:
    events = [analysed(f"evt_{index}") for index in range(10)]

    briefing = build_briefing(events, ARTICLES, day=DAY, limit=3)

    assert len(briefing.stories) == 3


def test_sources_are_deduplicated_by_publisher() -> None:
    """Four links from one outlet prove nothing that one does."""
    briefing = build_briefing(
        [analysed(article_ids=["a1", "a3", "a2"])], ARTICLES, day=DAY, limit=5
    )

    story = briefing.stories[0]
    assert [source.source_id for source in story.sources] == ["google-deepmind", "the-verge-ai"]
    assert story.source_count == 2


def test_a_story_carried_over_from_an_earlier_day_is_marked_developing() -> None:
    briefing = build_briefing([analysed(first_seen=MONDAY)], ARTICLES, day=DAY, limit=5)

    assert briefing.stories[0].is_developing is True


def test_a_day_with_nothing_verified_produces_an_empty_briefing() -> None:
    briefing = build_briefing([analysed(with_analysis=False)], ARTICLES, day=DAY, limit=5)

    assert briefing.is_empty
    assert briefing.lead is None


# --- Telegram rendering -------------------------------------------------------


def story(headline: str = "A headline", *, body: str = "Something happened.") -> Story:
    return Story(
        event_id="evt_1",
        headline=headline,
        what_happened=body,
        why_it_matters="It matters.",
        category=Category.MODEL_RELEASE,
        score=7.0,
        confidence=0.9,
        sources=[Source(source_id="openai", title="T", url="https://openai.com/a")],
        first_seen=NOW,
        last_updated=NOW,
    )


def briefing_of(*stories: Story, stats: BriefingStats | None = None) -> Briefing:
    return Briefing(
        day=DAY,
        generated_at=NOW,
        stories=list(stories),
        stats=stats or BriefingStats(feeds_ok=22, articles=515, events=490),
    )


def test_the_message_contains_the_stories() -> None:
    message = render_telegram(briefing_of(story("Gemma 4 is out")))

    assert "Gemma 4 is out" in message
    assert "AI-PULSE" in message


def test_untrusted_text_is_escaped() -> None:
    """Headlines come from a model that was fed text from the open internet."""
    message = render_telegram(briefing_of(story("<b>fake bold</b> & <script>alert(1)</script>")))

    assert "<script>" not in message
    assert "&lt;script&gt;" in message
    assert "&amp;" in message


def test_source_urls_are_escaped_in_links() -> None:
    hostile = Story(
        event_id="evt_1",
        headline="Headline",
        what_happened="Body",
        why_it_matters="Matters",
        category=Category.OTHER,
        score=5.0,
        confidence=0.5,
        sources=[Source(source_id="evil", title="t", url='https://x.test/"><b>')],
        first_seen=NOW,
        last_updated=NOW,
    )

    message = render_telegram(briefing_of(hostile))

    assert '"><b>' not in message
    assert "&quot;" in message


def test_the_message_never_exceeds_the_api_limit() -> None:
    long_body = "word " * 400
    stories = [story(f"Story number {index}", body=long_body) for index in range(10)]

    message = render_telegram(briefing_of(*stories))

    assert len(message) <= TELEGRAM_MAX_CHARS


def test_a_long_lead_costs_the_tail_not_the_message() -> None:
    stories = [story("Lead", body="word " * 800)] + [story(f"S{i}") for i in range(5)]

    message = render_telegram(briefing_of(*stories))

    assert "Lead" in message
    assert len(message) <= TELEGRAM_MAX_CHARS


def test_the_lead_gets_more_detail_than_the_rest() -> None:
    message = render_telegram(briefing_of(story("Lead"), story("Second")))

    assert "Why it matters" in message
    assert message.index("Lead") < message.index("Second")


def test_an_empty_briefing_says_so_rather_than_pretending() -> None:
    message = render_telegram(briefing_of())

    assert "No story could be verified" in message
    assert len(message) <= TELEGRAM_MAX_CHARS


def test_the_footer_reports_failures() -> None:
    stats = BriefingStats(feeds_ok=20, feeds_failed=2, model_failures=3, events=100)

    message = render_telegram(briefing_of(story(), stats=stats))

    assert "2 failed" in message
    assert "3 model failures" in message


def test_rendering_is_deterministic() -> None:
    briefing = briefing_of(story("One"), story("Two"))

    assert render_telegram(briefing) == render_telegram(briefing)


def test_the_body_budget_leaves_headroom_under_the_api_limit() -> None:
    assert BODY_BUDGET < TELEGRAM_MAX_CHARS


# --- HTML rendering -----------------------------------------------------------


def test_the_page_is_self_contained() -> None:
    """No CDN, no build step, no JavaScript: that is what makes Pages sufficient."""
    page = render_html(briefing_of(story()))

    assert "<script" not in page
    assert "http://" not in page.replace("https://", "")
    assert "<style>" in page


def test_the_page_escapes_untrusted_text() -> None:
    page = render_html(briefing_of(story("<script>alert(1)</script>")))

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_page_links_every_source() -> None:
    page = render_html(briefing_of(story()))

    assert 'href="https://openai.com/a"' in page
    assert 'rel="noopener nofollow"' in page


def test_an_empty_day_renders_a_page_that_says_so() -> None:
    page = render_html(briefing_of())

    assert "No story could be verified" in page


def test_the_archive_lists_briefings_with_links() -> None:
    index = render_index([briefing_of(story("Today")), briefing_of(story("Yesterday"))])

    assert "Today" in index
    assert f'href="{DAY.isoformat()}.html"' in index
