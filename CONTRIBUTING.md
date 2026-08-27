# Contributing

This is a personal project with one maintainer. Issues and pull requests are welcome, but
be aware of what you are contributing to: a briefing tuned to one person's interests, by
design. A change that makes it more general is usually a change that makes it worse.

## Before you open a pull request

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pipeline fits together, and
`PLAN.md` §2 for why. Most of §2 exists because something failed in a way that was not
obvious, so a change that contradicts it is not automatically wrong — but it should say
which decision it is reversing and what it knows that the original did not.

One rule carries more weight than the rest:

> Deterministic code does the work, and the model is asked only for judgement it is
> genuinely better at.

A change that puts a model where arithmetic would do is going the wrong way, and a change
that spends more of a free tier's daily allowance needs to say what it buys.

## Running the checks

```bash
python -m venv .venv && .venv/Scripts/activate    # or source .venv/bin/activate
pip install -r requirements.lock
pip install -e . --no-deps

ruff check .
ruff format --check .
mypy
pytest
pytest --cov     # the coverage floor, as CI runs it
```

All of these run in CI on every push, along with `pip-audit` against the lockfiles and
`scripts/eval.py`, which fails the build if the structural injection corpus reports an
escape.

`--cov` is not in `addopts` on purpose: it would make running a single test file fail a
whole-project floor, which trains people to pass `--no-cov` and defeats the point.

Nothing in the test suite needs network access, a model, or credentials — if a change makes
a test need one of those, the change is what needs fixing.

## What a good pull request looks like

- **One thing.** A fix and a refactor in one diff cost more to review than both separately.
- **A test that fails without it.** Especially for anything in `app/ingestion/urlguard.py`,
  `app/ingestion/fetcher.py`, or `app/llm/prompts.py` — those three are the security
  boundary and have dedicated suites in `tests/security/`.
- **Comments that say why, not what.** The codebase explains its reasoning where the
  reasoning is not obvious, and stays quiet where it is.
- **No new dependency without a reason that survives a sentence.** Everything here runs on
  free tiers; each addition is a thing to pin, audit and upgrade forever.

## What will be declined

- Anything that opens the bot to people other than the owner beyond the existing read-only
  demonstration mode.
- Docker, Kubernetes, Postgres, a message queue, a FastAPI layer. Each was considered and
  cut; `PLAN.md` §31 is the argument. Unused infrastructure is a maintenance cost with no
  user.
- New feed sources that have not been checked with `python scripts/verify_sources.py`. A
  feed without real publication dates bypasses the recency window and quietly fills the
  briefing with old news.
- Filled-in evaluation labels. Those are the repository owner's judgement about whether a
  story mattered, and nobody else's to supply.

## Security

Do not open a public issue for a vulnerability. Private reporting is enabled — see
[docs/SECURITY.md](docs/SECURITY.md).
