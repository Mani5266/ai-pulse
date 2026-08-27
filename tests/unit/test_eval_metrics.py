"""Evaluation metrics: structural properties, and honesty about unlabelled ones."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from app.briefing.models import Briefing, Claim, Source, Story
from app.evals.metrics import (
    Dataset,
    LabelledEvent,
    label_sheet,
    measure_judgement,
    measure_structure,
)
from app.intelligence.categories import Category
from app.intelligence.verification import VerificationStatus

DAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def story(
    event_id: str = "evt_1",
    *,
    sources: list[str] | None = None,
    claims: list[Claim] | None = None,
    what_happened: str = "Something happened.",
    category: Category = Category.MODEL_RELEASE,
) -> Story:
    return Story(
        event_id=event_id,
        headline="A headline",
        what_happened=what_happened,
        why_it_matters="It matters.",
        category=category,
        score=7.0,
        confidence=0.9,
        sources=[
            Source(source_id=name, title="t", url=f"https://{name}.example.com/a")
            for name in (sources or ["openai"])
        ],
        claims=claims or [],
        first_seen=NOW,
        last_updated=NOW,
    )


def briefing(*stories: Story) -> Briefing:
    return Briefing(day=DAY, generated_at=NOW, stories=list(stories))


# --- structural ---------------------------------------------------------------


def test_a_sound_briefing_passes_every_structural_check() -> None:
    report = measure_structure([briefing(story(), story("evt_2"))])

    assert report.is_sound is True
    assert report.citation_rate == 1.0
    assert report.attribution_validity == 1.0


def test_a_story_without_a_source_is_caught() -> None:
    naked = story().model_copy(update={"sources": []})

    report = measure_structure([briefing(naked)])

    assert report.citation_rate == 0.0
    assert report.is_sound is False


def test_a_claim_attributed_to_an_absent_source_is_caught() -> None:
    """Verification is supposed to discard these, so anything below 100% is a bug."""
    bad = story(
        sources=["openai"],
        claims=[
            Claim(
                text="Confirmed elsewhere",
                status=VerificationStatus.VERIFIED,
                supported_by=["openai", "reuters"],
            )
        ],
    )

    report = measure_structure([briefing(bad)])

    assert report.attribution_validity == 0.0
    assert report.is_sound is False


def test_a_valid_attribution_passes() -> None:
    good = story(
        sources=["openai", "the-verge-ai"],
        claims=[
            Claim(
                text="Two outlets say so",
                status=VerificationStatus.VERIFIED,
                supported_by=["openai", "the-verge-ai"],
            )
        ],
    )

    report = measure_structure([briefing(good)])

    assert report.attribution_validity == 1.0
    assert report.corroborated_claims == 1


def test_the_same_event_twice_in_one_briefing_is_caught() -> None:
    """A stated V1 criterion: no obvious duplicate stories."""
    report = measure_structure([briefing(story("evt_1"), story("evt_1"))])

    assert report.duplicate_events_in_a_briefing == 1
    assert report.is_sound is False


def test_the_same_event_on_different_days_is_not_a_duplicate() -> None:
    """That is a developing story, which the product is built to show."""
    today = briefing(story("evt_1"))
    yesterday = Briefing(day=date(2026, 8, 25), generated_at=NOW, stories=[story("evt_1")])

    report = measure_structure([today, yesterday])

    assert report.duplicate_events_in_a_briefing == 0


def test_no_briefings_is_not_a_failure() -> None:
    report = measure_structure([])

    assert report.stories == 0
    assert report.attribution_validity == 1.0


# --- judgement ----------------------------------------------------------------


def test_unlabelled_metrics_report_pending_rather_than_zero() -> None:
    """A metric graded against the author's own guess measures nothing, and reporting
    0.0 would read as a result."""
    report = measure_judgement([briefing(story())], Dataset())

    assert report.is_pending is True
    assert report.precision is None
    assert report.category_accuracy is None


def test_precision_counts_only_stories_a_person_judged() -> None:
    dataset = Dataset(
        labelled=[
            LabelledEvent(event_id="evt_1", day="2026-08-26", headline="A", importance="important"),
            LabelledEvent(event_id="evt_2", day="2026-08-26", headline="B", importance="noise"),
            LabelledEvent(event_id="evt_3", day="2026-08-26", headline="C", importance="important"),
        ]
    )

    report = measure_judgement([briefing(story("evt_1"), story("evt_2"))], dataset)

    assert report.matched == 2
    assert report.precision == 0.5


def test_marginal_stories_count_in_neither_direction() -> None:
    dataset = Dataset(
        labelled=[
            LabelledEvent(event_id="evt_1", day="2026-08-26", headline="A", importance="important"),
            LabelledEvent(event_id="evt_2", day="2026-08-26", headline="B", importance="marginal"),
        ]
    )

    report = measure_judgement([briefing(story("evt_1"), story("evt_2"))], dataset)

    assert report.precision == 1.0


def test_category_accuracy_compares_only_corrected_categories() -> None:
    dataset = Dataset(
        labelled=[
            LabelledEvent(
                event_id="evt_1",
                day="2026-08-26",
                headline="A",
                importance="important",
                category=Category.RESEARCH,
            ),
            LabelledEvent(event_id="evt_2", day="2026-08-26", headline="B", importance="important"),
        ]
    )

    report = measure_judgement(
        [briefing(story("evt_1", category=Category.MODEL_RELEASE), story("evt_2"))], dataset
    )

    assert report.category_comparisons == 1
    assert report.category_accuracy == 0.0


# --- the label sheet ----------------------------------------------------------


def test_the_label_sheet_leaves_only_the_judgement_blank() -> None:
    sheet = json.loads(label_sheet([briefing(story("evt_1"))]))

    row = sheet["labelled"][0]
    assert row["event_id"] == "evt_1"
    assert row["headline"] == "A headline"
    assert row["category"] == "model_release"
    assert row["importance"] == ""


def test_the_label_sheet_explains_what_to_do() -> None:
    sheet = json.loads(label_sheet([briefing(story())]))

    assert "important" in sheet["instructions"]
    assert "dataset.json" in sheet["instructions"]


# --- a generated sheet is not a dataset ---------------------------------------


def test_an_unfilled_sheet_reports_nothing() -> None:
    """The bug this guards: the sheet arrives with `category` already filled in.

    Counting those rows graded the pipeline against its own answer and printed
    "category accuracy 100%" off a sheet nobody had touched.
    """
    sheet = json.loads(label_sheet([briefing(story("evt_1"), story("evt_2"))]))
    dataset = Dataset.model_validate(sheet)

    report = measure_judgement([briefing(story("evt_1"), story("evt_2"))], dataset)

    assert report.is_pending is True
    assert report.labelled == 0
    assert report.category_accuracy is None


def test_only_rows_with_an_importance_are_counted() -> None:
    dataset = Dataset(
        labelled=[
            LabelledEvent(
                event_id="evt_1",
                day=DAY.isoformat(),
                headline="A headline",
                importance="important",
                category=Category.MODEL_RELEASE,
            ),
            LabelledEvent(
                event_id="evt_2",
                day=DAY.isoformat(),
                headline="A headline",
                importance="",
                category=Category.MODEL_RELEASE,
            ),
        ]
    )

    report = measure_judgement([briefing(story("evt_1"), story("evt_2"))], dataset)

    assert report.labelled == 1
    assert report.matched == 1


def test_an_importance_is_read_regardless_of_case_or_padding() -> None:
    """A hand-edited file is not a form. ' Important ' is a judgement."""
    dataset = Dataset(
        labelled=[
            LabelledEvent(
                event_id="evt_1",
                day=DAY.isoformat(),
                headline="A headline",
                importance="  Important  ",
            )
        ]
    )

    report = measure_judgement([briefing(story("evt_1"))], dataset)

    assert report.labelled == 1
    assert report.important_published == 1


def test_an_unrecognised_importance_is_not_a_judgement() -> None:
    dataset = Dataset(
        labelled=[
            LabelledEvent(
                event_id="evt_1",
                day=DAY.isoformat(),
                headline="A headline",
                importance="TODO",
            )
        ]
    )

    assert measure_judgement([briefing(story("evt_1"))], dataset).is_pending is True
