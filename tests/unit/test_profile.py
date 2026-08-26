"""Profile loading tests, including the profile the pipeline actually uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ConfigError
from app.intelligence.categories import Category
from app.ranking.profile import DEFAULT_PROFILE_PATH, Profile, load_profile

VALID = """
interests:
  - agent
  - open weights
low_interest:
  - earnings
category_weights:
  model_release: 1.0
  funding: 0.25
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_valid_profile(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, VALID))

    assert "agent" in profile.interests
    assert "earnings" in profile.low_interest
    assert profile.weight_for(Category.MODEL_RELEASE) == 1.0


def test_terms_are_normalised_and_deduplicated(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, "interests:\n  - Agent\n  - agent\n  - '  AGENT '\n"))

    assert profile.interests == ("agent",)


def test_an_unlisted_category_gets_a_middling_weight() -> None:
    profile = Profile(category_weights={Category.MODEL_RELEASE: 1.0})

    assert 0.0 < profile.weight_for(Category.SECURITY) < 1.0


def test_matching_is_whole_word_only() -> None:
    profile = Profile(interests=("act",))

    assert profile.matches("The act passed") == (1, 0)
    assert profile.matches("Accelerating inference") == (0, 0)


def test_matching_counts_both_kinds_of_term() -> None:
    profile = Profile(interests=("agent", "inference"), low_interest=("earnings",))

    assert profile.matches("agent inference earnings") == (2, 1)


def test_matching_is_case_insensitive() -> None:
    assert Profile(interests=("agent",)).matches("AGENT frameworks") == (1, 0)


def test_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_profile(tmp_path / "absent.yaml")


def test_malformed_yaml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_profile(write(tmp_path, "interests: [unclosed"))


def test_a_non_mapping_profile_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_profile(write(tmp_path, "- just\n- a\n- list\n"))


def test_an_out_of_range_weight_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="between 0 and 1"):
        load_profile(write(tmp_path, "category_weights:\n  model_release: 4.0\n"))


def test_an_unknown_category_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid profile"):
        load_profile(write(tmp_path, "category_weights:\n  gossip: 0.5\n"))


def test_the_shipped_profile_is_valid() -> None:
    profile = load_profile(DEFAULT_PROFILE_PATH)

    assert profile.interests
    assert profile.low_interest
    # Every category should carry a deliberate weight rather than falling back.
    assert set(profile.category_weights) == set(Category)
