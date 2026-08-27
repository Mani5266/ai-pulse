"""Pipeline metrics measured against a labelled dataset and against live output.

Three of these need no human judgement and one does, and keeping the two kinds apart is
the point of this module.

**Structural metrics** are properties the pipeline must satisfy whatever anyone thinks of
the news: no story cites a source its event does not have, no claim is corroborated by a
publisher that never appeared, no briefing repeats an event. These run on real committed
output, every day, and a failure is a bug.

**Judgement metrics** — was this story actually important, is this category right — need
labels from a person. They are reported as *pending* until those labels exist rather than
being approximated, because a metric the author graded against their own guess measures
nothing and a reader who works that out discounts everything else on the page.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.briefing.models import Briefing
from app.intelligence.categories import Category
from app.intelligence.verification import VerificationStatus

logger = logging.getLogger(__name__)

DEFAULT_DATASET = Path("evals/dataset.json")


JUDGEMENTS: frozenset[str] = frozenset({"important", "marginal", "noise"})
"""The only values that count as a person having looked at a row."""


class LabelledEvent(BaseModel):
    """One human judgement about one event."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    day: str
    headline: str
    importance: str
    """One of: important, marginal, noise. The reader's call, not the pipeline's.

    Empty until somebody fills it in, which is what :attr:`is_judged` tests."""

    category: Category | None = None
    """The category a reader would have given it.

    The sheet pre-fills this with the pipeline's own answer, because a person cannot
    correct a category they cannot see. That makes it worthless as evidence on its own —
    see :attr:`is_judged`."""

    notes: str = ""

    @property
    def judgement(self) -> str:
        """The importance, normalised. A hand-edited file is not a form."""
        return self.importance.strip().lower()

    @property
    def is_judged(self) -> bool:
        """Has a person actually ruled on this row?

        Only ``importance`` proves it. ``category`` arrives pre-filled from the pipeline,
        so counting a row because its category is set would grade the pipeline against its
        own guess — an unfilled sheet reported 100% category accuracy before this existed,
        which is precisely the number this project refuses to publish.
        """
        return self.judgement in JUDGEMENTS


class Dataset(BaseModel):
    """The labelled set, however far it has got."""

    model_config = ConfigDict(frozen=True)

    labelled: list[LabelledEvent] = Field(default_factory=list)

    @property
    def judged(self) -> list[LabelledEvent]:
        """Only the rows a person has ruled on. A generated sheet is not a dataset."""
        return [item for item in self.labelled if item.is_judged]

    @property
    def is_empty(self) -> bool:
        return not self.judged


class StructuralReport(BaseModel):
    """Properties that hold or do not, measured on real briefings."""

    model_config = ConfigDict(frozen=True)

    briefings: int = 0
    stories: int = 0

    stories_with_sources: int = 0
    claims_total: int = 0
    claims_with_valid_attribution: int = 0
    corroborated_claims: int = 0
    duplicate_events_in_a_briefing: int = 0
    stories_without_summary: int = 0

    @property
    def citation_rate(self) -> float:
        """Share of published stories that link at least one source."""
        return self.stories_with_sources / self.stories if self.stories else 0.0

    @property
    def attribution_validity(self) -> float:
        """Share of claims whose every attributed source belongs to the event.

        Anything below 1.0 is a bug: verification is supposed to discard the rest.
        """
        return self.claims_with_valid_attribution / self.claims_total if self.claims_total else 1.0

    @property
    def is_sound(self) -> bool:
        return (
            self.citation_rate == 1.0
            and self.attribution_validity == 1.0
            and self.duplicate_events_in_a_briefing == 0
            and self.stories_without_summary == 0
        )


class JudgementReport(BaseModel):
    """Metrics that need a person, reported honestly when nobody has labelled anything."""

    model_config = ConfigDict(frozen=True)

    labelled: int = 0
    matched: int = 0
    important_published: int = 0
    noise_published: int = 0
    category_agreements: int = 0
    category_comparisons: int = 0

    @property
    def is_pending(self) -> bool:
        return self.labelled == 0

    @property
    def precision(self) -> float | None:
        """Of the published stories a person labelled, how many were worth publishing.

        None until there are labels. Not 0.0, and not a guess.
        """
        judged = self.important_published + self.noise_published
        return self.important_published / judged if judged else None

    @property
    def category_accuracy(self) -> float | None:
        if not self.category_comparisons:
            return None
        return self.category_agreements / self.category_comparisons


def load_dataset(path: Path | None = None) -> Dataset:
    """Read the labelled dataset. Absent means unlabelled, which is a state, not an error."""
    dataset_path = path or DEFAULT_DATASET
    if not dataset_path.exists():
        return Dataset()
    try:
        return Dataset.model_validate_json(dataset_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        logger.warning("%s: unreadable dataset: %s", dataset_path, exc)
        return Dataset()


def measure_structure(briefings: Sequence[Briefing]) -> StructuralReport:
    """Check the properties that must hold on every published briefing."""
    stories = 0
    with_sources = 0
    claims = 0
    valid_attribution = 0
    corroborated = 0
    duplicates = 0
    without_summary = 0

    for briefing in briefings:
        seen: set[str] = set()
        for story in briefing.stories:
            stories += 1
            if story.event_id in seen:
                duplicates += 1
            seen.add(story.event_id)

            if story.sources:
                with_sources += 1
            if not story.what_happened.strip():
                without_summary += 1

            known = {source.source_id.lower() for source in story.sources}
            for claim in story.claims:
                claims += 1
                attributed = {source.lower() for source in claim.supported_by}
                if attributed <= known:
                    valid_attribution += 1
                if claim.status is VerificationStatus.VERIFIED:
                    corroborated += 1

    return StructuralReport(
        briefings=len(briefings),
        stories=stories,
        stories_with_sources=with_sources,
        claims_total=claims,
        claims_with_valid_attribution=valid_attribution,
        corroborated_claims=corroborated,
        duplicate_events_in_a_briefing=duplicates,
        stories_without_summary=without_summary,
    )


def measure_judgement(briefings: Sequence[Briefing], dataset: Dataset) -> JudgementReport:
    """Compare what was published against what a person said was worth publishing."""
    if dataset.is_empty:
        return JudgementReport()

    labels = {item.event_id: item for item in dataset.judged}
    published = {story.event_id: story for briefing in briefings for story in briefing.stories}

    matched = 0
    important = 0
    noise = 0
    agreements = 0
    comparisons = 0

    for event_id, story in published.items():
        label = labels.get(event_id)
        if label is None:
            continue
        matched += 1
        if label.judgement == "important":
            important += 1
        elif label.judgement == "noise":
            noise += 1
        if label.category is not None:
            comparisons += 1
            if label.category is story.category:
                agreements += 1

    return JudgementReport(
        labelled=len(dataset.judged),
        matched=matched,
        important_published=important,
        noise_published=noise,
        category_agreements=agreements,
        category_comparisons=comparisons,
    )


def label_sheet(briefings: Sequence[Briefing]) -> str:
    """A file to hand a person, pre-filled with everything except the judgement.

    Every field is populated but ``importance``, which is the one thing a person has to
    supply. Making it easy is the difference between a dataset that exists and one that
    stays a good intention.
    """
    rows = [
        {
            "event_id": story.event_id,
            "day": briefing.day.isoformat(),
            "headline": story.headline,
            "importance": "",
            "category": story.category.value,
            "notes": "",
        }
        for briefing in briefings
        for story in briefing.stories
    ]
    return json.dumps(
        {
            "instructions": (
                "Set importance on each row to important, marginal or noise. Change "
                "category only where the pipeline got it wrong. Save as evals/dataset.json "
                "with the rows under a 'labelled' key."
            ),
            "labelled": rows,
        },
        indent=2,
        ensure_ascii=False,
    )
