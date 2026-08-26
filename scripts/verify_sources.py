"""Check every feed in the registry and report which ones are usable.

Run before trusting the registry, and again whenever a source starts producing nothing::

    python scripts/verify_sources.py
    python scripts/verify_sources.py --include-disabled

Feeds die quietly. A publisher retires its RSS service and the pipeline simply sees fewer
articles, with no error anywhere. This script makes that visible: it fetches each feed
through the same SSRF-guarded fetcher the pipeline uses, parses it, and prints the entry
count and the most recent item date.

Exit code is 1 if any enabled source fails, so it can gate CI later.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import AIPulseError
from app.core.models import Source
from app.ingestion.feeds import parse_feed
from app.ingestion.fetcher import SafeFetcher
from app.ingestion.sources import load_sources


def check(fetcher: SafeFetcher, source: Source, settings: Settings) -> tuple[bool, str]:
    """Return (ok, human-readable detail) for one source."""
    try:
        response = fetcher.get(str(source.feed_url))
    except AIPulseError as exc:
        return False, f"{type(exc).__name__}: {exc}"

    articles = parse_feed(
        source,
        response.content,
        fetched_at=datetime.now(UTC),
        max_chars=settings.max_article_chars,
    )
    if not articles:
        return False, f"HTTP {response.status_code}, {response.size_bytes} bytes, 0 entries"

    dated = [article.published_at for article in articles if article.published_at]
    newest = max(dated).date().isoformat() if dated else "no dates"
    return True, f"{len(articles):>3} entries, newest {newest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="also check sources marked enabled: false",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    sources = load_sources()
    if not args.include_disabled:
        sources = [source for source in sources if source.enabled]

    failures: list[str] = []
    width = max(len(source.id) for source in sources)

    with SafeFetcher(settings) as fetcher:
        for source in sources:
            ok, detail = check(fetcher, source, settings)
            mark = "ok  " if ok else "FAIL"
            flag = "" if source.enabled else "  (disabled)"
            print(f"{mark}  {source.id:<{width}}  {detail}{flag}")
            if not ok and source.enabled:
                failures.append(source.id)

    print(f"\n{len(sources) - len(failures)}/{len(sources)} usable")
    if failures:
        print("failing: " + ", ".join(failures))
        print("Set these to `enabled: false` in config/sources.yaml with a note.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
