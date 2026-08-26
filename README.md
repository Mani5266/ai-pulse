# AI-Pulse

[![CI](https://github.com/Mani5266/ai-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/ai-pulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A multi-source event intelligence pipeline for AI news. It ingests ~500 articles a day
from ~25 RSS feeds, deduplicates them, clusters them into distinct **events**,
cross-checks each event's claims against independent sources, ranks them deterministically,
and publishes a short evidence-backed briefing.

> Replace `OWNER` in the badge URL above with your GitHub username once the repository is
> pushed, and delete this line.

## Why this is not a news summarizer

A summarizer treats the **article** as the unit of work and produces prose. AI-Pulse
treats the **event** as the unit of work, tracks how each event develops across days, and
attaches a verification status to every material claim.

| | Summarizer | AI-Pulse |
| --- | --- | --- |
| Unit of work | Article | Event (many articles) |
| Memory | None | Timeline across days |
| Claims | Asserted | `VERIFIED` / `PARTIALLY_VERIFIED` / `UNVERIFIED` / `CONTRADICTED` |
| LLM role | Does everything | Three impact scores and one editorial pass |

## Architecture

```
   RSS feeds (~25)
        |
   [ fetch ]        SSRF guard, timeouts, size caps, redirect limit
        |
   [ normalize ]    URL canonicalization, content hash
        |
   [ dedupe ]       URL hash, content hash, title trigram similarity
        |
   [ cluster ]      articles -> events
        |
   [ score:code ]   credibility, novelty, personal_relevance -> top 20
        |
   [ score:llm ]    technical / industry / developer impact
        |
   [ verify ]       claims cross-checked within the event cluster
        |
   [ rank ]         deterministic weighted score
        |
   [ edit ]         top 5 stories -> briefing
        |
   +----------------+----------------+
   |                                 |
GitHub Pages                     Telegram
```

Everything above `[ score:llm ]` is deterministic and unit-testable without a model.

Three design rules carry most of the weight:

1. **Deterministic filtering runs before the first model call.** At most 20 events reach
   the LLM, and the run is capped at 25 model calls, so the pipeline fits inside any free
   API tier.
2. **The LLM receives data, never authority.** No shell, no filesystem, no browser, no
   database write, no network. Article text is wrapped in `<DOCUMENT>` tags and the system
   prompt declares it untrusted. Every response is validated against a Pydantic schema.
3. **Git is the database.** Article and event records are committed as NDJSON, so the
   repository history *is* the timeline. A SQLite database is rebuilt from it on demand
   and is gitignored.

Full reasoning, including the rejected alternatives, is in [PLAN.md](PLAN.md).

## Stack

Python 3.11 · httpx · feedparser · Pydantic · Ruff · MyPy (strict) · pytest ·
GitHub Actions · GitHub Pages · Telegram Bot API · Ollama (local development)

Recurring cost: zero.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| P0 | Repository, tooling, CI | Done |
| P1 | Feed ingestion with SSRF protection | Done — 22/22 feeds live, 515 articles/run |
| P2 | Canonicalization and deduplication | Done — 3-pass, deterministic |
| P3 | Event clustering | Next |
| P4 | Deterministic scoring | |
| P5 | LLM provider layer | |
| P6 | Briefing, Telegram, Pages | |
| P7 | Claim verification | |
| P8 | Timeline | |
| P9 | Evaluation harness | |
| P10 | Observability | |

## Development

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"
cp .env.example .env            # then fill it in

ruff check .
ruff format --check .
mypy
pytest

python scripts/verify_sources.py   # check every feed is still alive
python -m app.jobs.daily_briefing
```

`.env` is gitignored and must never be committed. Only `.env.example` belongs in the
repository.

## License

MIT. See [LICENSE](LICENSE).
