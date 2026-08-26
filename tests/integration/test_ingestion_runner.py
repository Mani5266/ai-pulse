"""Ingestion orchestration: a failing source must never end the run."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from app.core.config import Settings
from app.core.models import Source, SourceTier
from app.ingestion.fetcher import SafeFetcher
from app.ingestion.runner import ingest_all, ingest_source, summarise
from app.ingestion.urlguard import IpAddress, Resolver

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Item one</title><link>https://good.example.com/one</link></item>
  <item><title>Item two</title><link>https://good.example.com/two</link></item>
</channel></rss>"""


def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def source(source_id: str, host: str) -> Source:
    return Source(
        id=source_id,
        name=source_id,
        tier=SourceTier.PRIMARY,
        feed_url=f"https://{host}/rss",  # type: ignore[arg-type]
        credibility=1.0,
    )


def public_resolver() -> Resolver:
    def resolve(host: str, port: int) -> list[IpAddress]:
        return [ipaddress.ip_address("93.184.216.34")]

    return resolve


def handler(request: httpx.Request) -> httpx.Response:
    host = request.headers["host"]
    if host == "good.example.com":
        return httpx.Response(200, content=RSS)
    if host == "empty.example.com":
        return httpx.Response(200, content=b"not a feed")
    return httpx.Response(500, content=b"boom")


def build_fetcher() -> SafeFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return SafeFetcher(settings(), client=client, resolver=public_resolver())


def test_one_dead_source_does_not_stop_the_others() -> None:
    sources = [
        source("dead", "dead.example.com"),
        source("good", "good.example.com"),
        source("empty", "empty.example.com"),
    ]

    with build_fetcher() as fetcher:
        results = ingest_all(sources, settings(), fetcher=fetcher)

    by_id = {result.source_id: result for result in results}

    assert by_id["good"].ok is True
    assert by_id["good"].article_count == 2
    assert by_id["dead"].ok is False
    assert by_id["dead"].error is not None
    assert "HTTP 500" in by_id["dead"].error
    # A 200 that parses to nothing is a success with zero articles, not a crash.
    assert by_id["empty"].ok is True
    assert by_id["empty"].article_count == 0


def test_summary_counts_every_outcome() -> None:
    sources = [source("dead", "dead.example.com"), source("good", "good.example.com")]

    with build_fetcher() as fetcher:
        results = ingest_all(sources, settings(), fetcher=fetcher)

    assert summarise(results) == {"sources": 2, "ok": 1, "failed": 1, "articles": 2}


def test_results_record_timing_for_the_run_log() -> None:
    with build_fetcher() as fetcher:
        results = ingest_all([source("good", "good.example.com")], settings(), fetcher=fetcher)

    assert results[0].duration_seconds is not None
    assert results[0].duration_seconds >= 0
    assert results[0].http_status == 200


def test_a_transient_failure_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two of 25 feeds failed on a live run and both had succeeded minutes earlier."""
    monkeypatch.setattr("app.ingestion.runner.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(403, content=b"bot protection")
        return httpx.Response(200, content=RSS)

    client = httpx.Client(transport=httpx.MockTransport(flaky), follow_redirects=False)
    fetcher = SafeFetcher(settings(), client=client, resolver=public_resolver())

    result = ingest_source(fetcher, source("good", "good.example.com"), settings())

    assert result.ok is True
    assert result.article_count == 2
    assert attempts["n"] == 2


def test_a_persistent_failure_gives_up_after_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingestion.runner.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def dead(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, content=b"boom")

    client = httpx.Client(transport=httpx.MockTransport(dead), follow_redirects=False)
    fetcher = SafeFetcher(settings(), client=client, resolver=public_resolver())

    result = ingest_source(fetcher, source("dead", "dead.example.com"), settings())

    assert result.ok is False
    assert attempts["n"] == 2


def test_an_ssrf_rejection_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a security control until it relents is not resilience."""
    monkeypatch.setattr("app.ingestion.runner.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, content=RSS)

    def private_resolver(host: str, port: int) -> list[IpAddress]:
        return [ipaddress.ip_address("10.0.0.5")]

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = SafeFetcher(settings(), client=client, resolver=private_resolver)

    result = ingest_source(fetcher, source("internal", "internal.example.com"), settings())

    assert result.ok is False
    assert attempts["n"] == 0
