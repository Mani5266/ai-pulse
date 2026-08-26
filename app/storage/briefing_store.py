"""Briefing persistence and site generation.

Briefings are stored as JSON, one file per day, and the static site is rebuilt from those
files rather than from the pipeline's memory. That separation is what makes the site
reproducible: the whole archive can be regenerated from committed data, with no model
calls and no network, which is also how a rendering change gets applied to history.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from app.briefing.models import Briefing
from app.briefing.render_html import render_html, render_index

logger = logging.getLogger(__name__)

BRIEFINGS_DIR = "briefings"


def briefing_path(data_dir: Path, day: date) -> Path:
    return data_dir / BRIEFINGS_DIR / f"{day.isoformat()}.json"


def write_briefing(data_dir: Path, briefing: Briefing) -> Path:
    """Persist one briefing. This happens before delivery is attempted."""
    path = briefing_path(data_dir, briefing.day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = briefing.model_dump(mode="json", exclude_none=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_briefing(data_dir: Path, day: date) -> Briefing | None:
    """Read one day's briefing, or None if there is not one."""
    path = briefing_path(data_dir, day)
    if not path.exists():
        return None
    try:
        return Briefing.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        logger.warning("%s: unreadable briefing: %s", path, exc)
        return None


def all_briefings(data_dir: Path) -> list[Briefing]:
    """Every stored briefing, newest first."""
    directory = data_dir / BRIEFINGS_DIR
    if not directory.exists():
        return []

    briefings: list[Briefing] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            briefings.append(Briefing.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            logger.warning("%s: skipping unreadable briefing: %s", path, exc)
    return briefings


def build_site(data_dir: Path, site_dir: Path) -> int:
    """Regenerate the whole static site from stored briefings.

    Returns the number of pages written. The site directory is generated output and is
    gitignored; GitHub Actions publishes it to Pages.
    """
    briefings = all_briefings(data_dir)
    if not briefings:
        logger.info("no briefings to publish")
        return 0

    site_dir.mkdir(parents=True, exist_ok=True)

    for briefing in briefings:
        (site_dir / f"{briefing.day.isoformat()}.html").write_text(
            render_html(briefing), encoding="utf-8"
        )

    # The newest briefing is also the landing page, so a bare link always shows today.
    (site_dir / "index.html").write_text(render_html(briefings[0]), encoding="utf-8")
    (site_dir / "archive.html").write_text(render_index(briefings), encoding="utf-8")

    logger.info("site rebuilt: %d briefings", len(briefings))
    return len(briefings) + 2
