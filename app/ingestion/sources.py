"""Source registry loading.

Sources live in ``config/sources.yaml`` rather than in code so that adding a feed is a
data change, reviewable as a one-line diff, with no import cycle and no redeploy.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.core.errors import ConfigError
from app.core.models import Source, SourceTier

DEFAULT_REGISTRY_PATH = Path("config/sources.yaml")


def load_sources(path: Path | None = None) -> list[Source]:
    """Load and validate every source in the registry, enabled or not.

    Raises :class:`~app.core.errors.ConfigError` on a malformed file or a duplicate id.
    A broken registry is a programming error, not a runtime condition, so it fails loudly
    rather than silently yielding fewer sources.
    """
    registry_path = path or DEFAULT_REGISTRY_PATH

    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"source registry not found: {registry_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"source registry is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "sources" not in raw:
        raise ConfigError(f"{registry_path}: expected a top-level 'sources' key")

    entries: Any = raw["sources"]
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{registry_path}: 'sources' must be a non-empty list")

    sources: list[Source] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        try:
            source = Source.model_validate(entry)
        except ValidationError as exc:
            raise ConfigError(f"{registry_path}: source #{index} is invalid: {exc}") from exc
        if source.id in seen:
            raise ConfigError(f"{registry_path}: duplicate source id {source.id!r}")
        seen.add(source.id)
        sources.append(source)

    return sources


def enabled_sources(sources: Iterable[Source]) -> list[Source]:
    """Filter to the sources the pipeline should actually fetch."""
    return [source for source in sources if source.enabled]


def credibility_by_id(sources: Iterable[Source]) -> dict[str, float]:
    """Lookup table for the deterministic credibility sub-score used in P4."""
    return {source.id: source.credibility for source in sources}


def by_tier(sources: Iterable[Source], tier: SourceTier) -> list[Source]:
    """All sources in one tier."""
    return [source for source in sources if source.tier is tier]
