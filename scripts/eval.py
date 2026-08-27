"""Run the evaluation and write the numbers the README quotes.

    python scripts/eval.py                 structural checks only, offline and free
    python scripts/eval.py --with-model    also run the corpus against the provider
    python scripts/eval.py --label-sheet   emit a sheet to fill in by hand

The default runs without a model on purpose: those checks are deterministic, cost nothing
and need no key, so they can gate every commit. The model layer needs a quota and is run
deliberately.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from app.core.config import Settings
from app.evals.injection import evaluate, load_corpus
from app.evals.metrics import (
    JudgementReport,
    StructuralReport,
    label_sheet,
    load_dataset,
    measure_judgement,
    measure_structure,
)
from app.llm.provider import LLMError, build_provider
from app.storage.briefing_store import all_briefings

RESULTS = Path("evals/results.json")


def _format(structure: StructuralReport, judgement: JudgementReport, injection: str) -> str:
    lines = [
        "AI-Pulse evaluation",
        "",
        f"  briefings measured        {structure.briefings}",
        f"  stories                   {structure.stories}",
        f"  stories citing a source   {structure.citation_rate:.0%}",
        f"  claims                    {structure.claims_total}"
        f" ({structure.corroborated_claims} corroborated)",
        f"  valid attributions        {structure.attribution_validity:.0%}",
        f"  duplicate events          {structure.duplicate_events_in_a_briefing}",
        f"  structurally sound        {'yes' if structure.is_sound else 'NO'}",
        "",
        f"  injection corpus          {injection}",
        "",
    ]

    if judgement.is_pending:
        lines += [
            "  precision                 pending labels",
            "  category accuracy         pending labels",
            "",
            "  No event has been labelled yet. Run with --label-sheet, fill in the",
            "  importance column, and save it as evals/dataset.json. These numbers are",
            "  left blank rather than estimated: a metric graded against the author's own",
            "  guess measures nothing.",
        ]
    else:
        precision = judgement.precision
        accuracy = judgement.category_accuracy
        lines += [
            f"  labelled events           {judgement.labelled}",
            f"  precision                 {precision:.0%}"
            if precision is not None
            else "  precision                 no judged stories yet",
            f"  category accuracy         {accuracy:.0%}"
            if accuracy is not None
            else "  category accuracy         no categories corrected",
        ]
        if not judgement.is_owner_judgement:
            lines += [
                "",
                f"  These labels were written by {judgement.labelled_by}, not by the",
                "  repository owner. Precision asks whether the pipeline picked what this",
                "  reader wanted, so treat the figure as a draft to correct rather than a",
                "  measurement to publish.",
            ]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-model", action="store_true", help="also exercise the provider")
    parser.add_argument("--label-sheet", action="store_true", help="emit a sheet to fill in")
    args = parser.parse_args(argv)

    # Headlines carry characters a Windows console cannot encode — a non-breaking hyphen
    # was enough to abort --label-sheet with a UnicodeEncodeError, and a shell redirect
    # inherits the same cp1252 default, so the sheet was written empty. CI is Linux and
    # UTF-8, so nothing here would ever have caught it.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = Settings()
    briefings = all_briefings(settings.data_dir)

    if args.label_sheet:
        print(label_sheet(briefings))
        return 0

    provider = None
    if args.with_model:
        try:
            provider = build_provider(settings)
        except LLMError as exc:
            print(f"model unavailable, running structural checks only: {exc}", file=sys.stderr)

    report = evaluate(load_corpus(), provider)
    structure = measure_structure(briefings)
    judgement = measure_judgement(briefings, load_dataset())

    print(_format(structure, judgement, report.summary()))

    if report.escapes:
        print("\nEscapes:")
        for result in report.results:
            if result.escaped:
                print(f"  {result.id} ({result.category}): {result.reason}")

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(
            {
                "structural": structure.model_dump(mode="json"),
                "judgement": judgement.model_dump(mode="json"),
                "injection": {
                    "total": report.total,
                    "escapes": report.escapes,
                    "escape_rate": round(report.escape_rate, 4),
                    "structural_escapes": report.structural_escapes,
                    "model_escapes": report.model_escapes,
                    "model_attacks_run": report.model_attacks_run,
                    "by_category": report.by_category(),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # A structural failure is a bug in this repository, so it fails the command.
    return 0 if structure.is_sound and report.structural_escapes == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
