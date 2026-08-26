"""Personal relevance profile.

Taste lives in ``config/profile.yaml`` rather than in code, for the same reason the source
registry does: changing what you care about should be a one-line data diff, not a code
change, and it should be visible to anyone reading the repository.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ConfigError
from app.intelligence.categories import Category

DEFAULT_PROFILE_PATH = Path("config/profile.yaml")


class Profile(BaseModel):
    """What the reader is interested in, and how much each category is worth."""

    model_config = ConfigDict(frozen=True)

    interests: tuple[str, ...] = ()
    low_interest: tuple[str, ...] = ()
    category_weights: dict[Category, float] = Field(default_factory=dict)

    @field_validator("interests", "low_interest")
    @classmethod
    def _normalise_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip().lower() for value in values if value.strip()}))

    @field_validator("category_weights")
    @classmethod
    def _check_weights(cls, weights: dict[Category, float]) -> dict[Category, float]:
        for category, weight in weights.items():
            if not 0.0 <= weight <= 1.0:
                raise ValueError(f"weight for {category} must be between 0 and 1, got {weight}")
        return weights

    def weight_for(self, category: Category) -> float:
        """Weight of a category, defaulting to the middle when unlisted.

        An unlisted category is neither promoted nor buried: a new category added to the
        enum should not silently disappear from the briefing.
        """
        return self.category_weights.get(category, 0.5)

    def matches(self, text: str) -> tuple[int, int]:
        """Count interest and low-interest terms present in ``text``.

        Whole-word matching, because substring matching is how "act" ends up inside
        "Accelerating" — the same defect already found in the categoriser.
        """
        lowered = text.lower()
        interest_hits = sum(1 for term in self.interests if _contains(lowered, term))
        low_hits = sum(1 for term in self.low_interest if _contains(lowered, term))
        return interest_hits, low_hits


@lru_cache(maxsize=256)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(term) + r"\b")


def _contains(text: str, term: str) -> bool:
    return _term_pattern(term).search(text) is not None


def load_profile(path: Path | None = None) -> Profile:
    """Load the profile, or raise :class:`~app.core.errors.ConfigError`."""
    profile_path = path or DEFAULT_PROFILE_PATH

    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"profile not found: {profile_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"profile is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{profile_path}: expected a mapping at the top level")

    try:
        return Profile.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{profile_path}: invalid profile: {exc}") from exc
