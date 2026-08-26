"""Source registry tests, including a check of the registry the pipeline actually uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ConfigError
from app.core.models import SourceTier
from app.ingestion.sources import (
    DEFAULT_REGISTRY_PATH,
    by_tier,
    credibility_by_id,
    enabled_sources,
    load_sources,
)

VALID = """
sources:
  - id: openai
    name: OpenAI
    tier: primary
    feed_url: https://openai.com/news/rss.xml
    credibility: 1.0
  - id: retired
    name: Retired Feed
    tier: journalism
    feed_url: https://example.com/rss
    credibility: 0.9
    enabled: false
    note: publisher retired its RSS service
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_enabled_and_disabled_sources(tmp_path: Path) -> None:
    sources = load_sources(write(tmp_path, VALID))

    assert [source.id for source in sources] == ["openai", "retired"]
    assert [source.id for source in enabled_sources(sources)] == ["openai"]


def test_credibility_lookup(tmp_path: Path) -> None:
    sources = load_sources(write(tmp_path, VALID))

    assert credibility_by_id(sources) == {"openai": 1.0, "retired": 0.9}


def test_filter_by_tier(tmp_path: Path) -> None:
    sources = load_sources(write(tmp_path, VALID))

    assert [source.id for source in by_tier(sources, SourceTier.PRIMARY)] == ["openai"]


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_sources(tmp_path / "absent.yaml")


def test_malformed_yaml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_sources(write(tmp_path, "sources: [unclosed"))


def test_missing_top_level_key_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="top-level 'sources' key"):
        load_sources(write(tmp_path, "feeds: []"))


def test_empty_source_list_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-empty list"):
        load_sources(write(tmp_path, "sources: []"))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    text = (
        VALID
        + """
  - id: openai
    name: OpenAI Again
    tier: primary
    feed_url: https://openai.com/news/rss.xml
    credibility: 1.0
"""
    )
    with pytest.raises(ConfigError, match="duplicate source id"):
        load_sources(write(tmp_path, text))


def _one_source(**fields: str) -> str:
    base = {
        "id": "broken",
        "name": "Broken",
        "tier": "primary",
        "feed_url": "https://example.com/rss",
        "credibility": "1.0",
    }
    base.update(fields)
    body = "\n".join(f"    {key}: {value}" for key, value in base.items())
    return "sources:\n  -\n" + body + "\n"


@pytest.mark.parametrize(
    "fields",
    [
        {"credibility": "1.5"},  # out of range
        {"credibility": "-0.1"},  # out of range
        {"tier": "gossip"},  # unknown tier
        {"feed_url": "not-a-url"},  # unparseable
        {"feed_url": "file:///etc/passwd"},  # not an HTTP URL
        {"id": "Has Spaces"},  # ids are used in filenames and logs
    ],
)
def test_invalid_source_fields_are_rejected(tmp_path: Path, fields: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="is invalid"):
        load_sources(write(tmp_path, _one_source(**fields)))


def test_shipped_registry_is_valid() -> None:
    """The registry the pipeline loads must parse, and must not be empty."""
    sources = load_sources(DEFAULT_REGISTRY_PATH)

    assert len(enabled_sources(sources)) >= 15
    assert {source.tier for source in sources} == set(SourceTier)


def test_research_sources_are_capped() -> None:
    """arXiv publishes hundreds of papers a day and must not drown the run."""
    sources = by_tier(load_sources(DEFAULT_REGISTRY_PATH), SourceTier.RESEARCH)

    assert sources
    for source in sources:
        assert source.max_items_per_run <= 25


def test_disabled_sources_explain_themselves() -> None:
    """A disabled feed without a note is a mystery for the next reader."""
    for source in load_sources(DEFAULT_REGISTRY_PATH):
        if not source.enabled:
            assert source.note, f"{source.id} is disabled with no note"
