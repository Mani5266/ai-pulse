"""Feed parsing tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.models import Source, SourceTier
from app.ingestion.feeds import parse_feed, strip_html

FETCHED_AT = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Lab</title>
    <item>
      <title>Example Lab releases Model X</title>
      <link>https://example.com/blog/model-x</link>
      <pubDate>Tue, 25 Aug 2026 10:30:00 GMT</pubDate>
      <description>&lt;p&gt;Model X has a 1M token context.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Second post</title>
      <link>https://example.com/blog/second</link>
      <pubDate>Mon, 24 Aug 2026 09:00:00 GMT</pubDate>
      <description>Shorter note.</description>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <entry>
    <title>Atom entry</title>
    <link href="https://example.com/atom/one"/>
    <updated>2026-08-25T12:00:00Z</updated>
    <content type="html">&lt;p&gt;Full &lt;b&gt;content&lt;/b&gt; body.&lt;/p&gt;</content>
    <summary>Short summary.</summary>
  </entry>
</feed>
"""


def source(**overrides: object) -> Source:
    defaults: dict[str, object] = {
        "id": "example",
        "name": "Example Lab",
        "tier": SourceTier.PRIMARY,
        "feed_url": "https://example.com/rss",
        "credibility": 1.0,
    }
    defaults.update(overrides)
    return Source.model_validate(defaults)


def test_rss_entries_become_articles() -> None:
    articles = parse_feed(source(), RSS, fetched_at=FETCHED_AT)

    assert len(articles) == 2
    first = articles[0]
    assert first.title == "Example Lab releases Model X"
    assert str(first.url) == "https://example.com/blog/model-x"
    assert first.source_id == "example"
    assert first.fetched_at == FETCHED_AT
    assert first.published_at is not None
    assert first.published_at.date().isoformat() == "2026-08-25"


def test_html_is_stripped_from_summaries() -> None:
    articles = parse_feed(source(), RSS, fetched_at=FETCHED_AT)

    assert articles[0].summary == "Model X has a 1M token context."


def test_atom_content_is_preferred_over_summary() -> None:
    articles = parse_feed(source(), ATOM, fetched_at=FETCHED_AT)

    assert len(articles) == 1
    assert articles[0].content == "Full content body."
    assert articles[0].summary == "Short summary."


def test_max_items_per_run_is_respected() -> None:
    articles = parse_feed(source(max_items_per_run=1), RSS, fetched_at=FETCHED_AT)

    assert len(articles) == 1


def test_content_is_truncated_to_the_cap() -> None:
    long_body = b"<description>" + b"a" * 5000 + b"</description>"
    feed = RSS.replace(b"<description>Shorter note.</description>", long_body)

    articles = parse_feed(source(), feed, fetched_at=FETCHED_AT, max_chars=100)

    assert articles[1].content is not None
    assert len(articles[1].content) == 100


def test_entries_without_a_link_are_skipped() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>No link here</title></item>
      <item><title>Has link</title><link>https://example.com/ok</link></item>
    </channel></rss>"""

    articles = parse_feed(source(), feed, fetched_at=FETCHED_AT)

    assert [article.title for article in articles] == ["Has link"]


def test_entries_with_a_non_http_link_are_skipped() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Bad scheme</title><link>javascript:alert(1)</link></item>
    </channel></rss>"""

    assert parse_feed(source(), feed, fetched_at=FETCHED_AT) == []


def test_garbage_input_yields_no_articles_and_does_not_raise() -> None:
    assert parse_feed(source(), b"not xml at all", fetched_at=FETCHED_AT) == []
    assert parse_feed(source(), b"", fetched_at=FETCHED_AT) == []


def test_truncated_xml_recovers_what_it_can_without_raising() -> None:
    """feedparser is lenient by design; a half-delivered feed is not a crash."""
    articles = parse_feed(source(), RSS[: len(RSS) // 2], fetched_at=FETCHED_AT)

    assert len(articles) <= 2
    assert all(article.source_id == "example" for article in articles)


def test_missing_dates_are_tolerated() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Undated</title><link>https://example.com/undated</link></item>
    </channel></rss>"""

    articles = parse_feed(source(), feed, fetched_at=FETCHED_AT)

    assert articles[0].published_at is None


def test_strip_html_handles_none_and_empty() -> None:
    assert strip_html(None) is None
    assert strip_html("") is None
    assert strip_html("   ") is None
    assert strip_html("<p>  spaced   out </p>") == "spaced out"
