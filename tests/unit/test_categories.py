"""Categorisation tests."""

from __future__ import annotations

import pytest

from app.intelligence.categories import Category, classify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Introducing Gemini 3.6 Flash", Category.MODEL_RELEASE),
        ("Anthropic raises $2B at a $60B valuation", Category.FUNDING),
        ("Nvidia acquires an inference startup", Category.ACQUISITION),
        ("EU passes the AI Act", Category.POLICY),
        ("New prompt injection attack on agent frameworks", Category.SECURITY),
        ("Our approach to alignment and interpretability", Category.SAFETY),
        ("New leaderboard results on the reasoning benchmark", Category.BENCHMARK),
        ("We open source the weights of our 7B model", Category.OPEN_SOURCE),
        ("Building an agentic workflow with tool use", Category.AI_AGENTS),
        ("New GPU cluster for large training runs", Category.INFRASTRUCTURE),
    ],
)
def test_headlines_land_in_the_right_category(title: str, expected: Category) -> None:
    assert classify(title) == expected


def test_keywords_match_whole_words_only() -> None:
    """Real defect: the policy keyword "act" matched inside "Accelerating", so a Gemini
    engineering post was filed as policy news."""
    assert classify("Accelerating Gemini Nano models on Pixel") is not Category.POLICY
    assert classify("Measuring the impact of retrieval") is not Category.POLICY
    assert classify("Best practice for fine-tuning") is not Category.POLICY


def test_arxiv_sources_are_always_research() -> None:
    """A paper announcing a model is still a paper."""
    assert classify("Introducing a new model", source_id="arxiv-cs-ai") is Category.RESEARCH
    assert classify("We release open weights", source_id="arxiv-cs-lg") is Category.RESEARCH


def test_more_specific_categories_win() -> None:
    """An acquisition that mentions a valuation is an acquisition."""
    assert classify("Nvidia acquires Foo at a $1B valuation") is Category.ACQUISITION


def test_unmatched_headlines_fall_through_to_other() -> None:
    assert classify("Our thoughts on the year ahead") is Category.OTHER


def test_empty_input_is_other() -> None:
    assert classify("") is Category.OTHER
    assert classify("   ", None) is Category.OTHER


def test_summary_contributes_to_the_decision() -> None:
    assert classify("A note from the team", "We are open sourcing the weights today.") is (
        Category.OPEN_SOURCE
    )
