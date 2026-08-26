# Tomorrow

Hand this file to a fresh session. It carries the state, the outstanding work, and the
context that is not obvious from the code.

---

## Where the project is

AI-Pulse is complete through P0–P10 and running unattended.

| | |
| --- | --- |
| Live site | https://mani5266.github.io/ai-pulse/ |
| Repository | https://github.com/Mani5266/ai-pulse (public) |
| Telegram bot | @Mani_aipulse_bot — `/latest`, `/refresh`, `/status`, `/help` |
| Daily run | GitHub Actions, 02:00 UTC (07:30 IST), commits `data/` back |
| Local bot | Windows task `AI-Pulse Bot`, starts at logon |
| Tests | 578 passing; ruff, mypy strict and the structural evaluation gate CI |

The plan and every design decision, including the ones that were wrong first, are in
`PLAN.md`. Read §2 before changing anything in the pipeline — most of those sections exist
because something failed in a way that was not obvious.

---

## Do these first, in order

### 1. Check the overnight run actually happened

```bash
gh run list --workflow="Daily briefing" --limit 3
curl -s -o /dev/null -w "%{http_code}\n" https://mani5266.github.io/ai-pulse/
git pull --rebase origin main          # collect the run's committed data
python scripts/eval.py                 # structural checks on the new briefing
```

This is the first day the 02:00 cron runs without anyone watching, so it is genuinely
unverified. If it failed, `data/runs/2026-08.ndjson` will hold a record saying why, and
`/status` in Telegram will report it.

### 2. Run the injection corpus against the model, in full

This is the number the README wants and it has **not been earned yet** — yesterday's run
reached only 2% of the corpus before the daily token allowance ran out.

```bash
python scripts/eval.py --with-model
```

Costs roughly 40 model calls. Do it before any other model work, while the day's quota is
untouched. If the report still says "only N% of the corpus reached it", say so rather than
quoting the figure.

### 3. Label events for the judgement metrics

`precision` and `category accuracy` are blank by design. They need a person, and that
person is the repository owner, not the model — the pipeline already ranks using
`config/profile.yaml`, so grading against it would measure the profile.

```bash
python scripts/eval.py --label-sheet > evals/dataset.json
```

Then edit `evals/dataset.json`: set `importance` on each row to `important`, `marginal` or
`noise`, and correct `category` only where the pipeline got it wrong. Roughly 20 minutes
once there are a few days of stories. `python scripts/eval.py` then computes the numbers.

**This step needs the owner. Do not fill in the labels on their behalf.**

---

## Hardening, in priority order

These are what stand between "runs today" and "still runs unattended in three months".
Each gap below was verified against the repository, not assumed. Roughly two hours for
items 1–3; half a day for 4–7.

Note first what is deliberately **not** on this list: Docker, Kubernetes, Postgres, a
message queue, a FastAPI layer, multi-region anything. Each adds a moving part with no
user. `PLAN.md` §31 is the argument, and unused infrastructure reads as cargo cult rather
than as rigour.

### 1. Pin the dependencies — the gap most likely to actually bite

`pyproject.toml` declares `httpx>=0.27` and four others the same way, so CI and the 02:00
run install whatever released that morning. One bad upstream release breaks the run and the
first sign is a missing briefing.

```bash
pip install uv && uv lock          # or pip-compile into requirements.lock
```

Commit the lockfile, install from it in both workflows, and add `.github/dependabot.yml`
so upgrades arrive as reviewable pull requests rather than as surprises.

### 2. Scan the dependency tree

No `pip-audit`, no Dependabot alerts configured. A public repository with an unaudited tree
is the first thing a security-minded reader checks.

```yaml
- run: pip install pip-audit && pip-audit
```

Add it to `ci.yml` after the tests.

### 3. Run CI on a schedule, to catch feed rot

Feeds die quietly — three did during this build, and one more turned into an HTML page.
`scripts/verify_sources.py` exists and exits non-zero on a dead feed, but only runs when
someone invokes it. A weekly `schedule:` trigger that runs it turns silent decay into a
failed build.

Keep it a separate job from the daily briefing: a dead feed should not fail a run that
otherwise produced a good briefing.

### 4. Wire a fallback model provider

Groq's free tier is the only provider configured for production. When its terms change,
every run degrades to the deterministic ranking — which works, and is the point of the
design, but produces a briefing with no prose. `LLMProvider` exists to make the second
provider cheap; it simply is not configured. Cerebras or OpenRouter, selected when the
first returns `DailyQuotaExceededError`.

### 5. Write the documents the layout already promises

`docs/ARCHITECTURE.md` and `docs/SECURITY.md` are named in the project structure and were
never written, and there is no `SECURITY.md` or `CONTRIBUTING.md` at the root. Most of the
content already exists in `PLAN.md` §2 and needs lifting rather than composing. For a
public repository this is the cheapest credibility available.

### 6. Measure coverage

578 tests is a count, not a claim about what is covered. `pytest-cov` with a floor in CI
turns it into one.

### 7. Alert on a degraded run, not only a failed one

GitHub emails the owner when a scheduled workflow fails, so an outright failure is not
silent. A run that *succeeds* while publishing two stories instead of five is silent, and
that is the more likely failure. The run records already hold everything needed; a check
that posts to Telegram when stories drop below a threshold would close it.

---

## Then, if there is time

1. **Clustering recall.** The known weakness, documented in `PLAN.md` §2.9. Two outlets
   describing one event in different words stay two events unless they name the same model
   version. `EventPair` in `app/llm/schemas.py` and `event_pair_prompt` in
   `app/llm/prompts.py` exist for this and are unused: shortlist candidate pairs
   deterministically, then ask the model to adjudicate a handful. Budget is the constraint.
2. **The Rundown feed 403s intermittently** under bot protection. The retry usually catches
   it. If `stats.html` shows it failing most days, disable it in `config/sources.yaml` with
   a note, the way the other dead feeds were handled.
3. **A second briefing depth.** The original brief wanted a five-minute version alongside
   the sixty-second one. All the data exists; it is a second renderer.

---

## Where this should be deployed

It already is, and the current arrangement is correct: Actions runs the pipeline, Pages
serves the site, git holds the data. There is no server that would make it better today,
and adding one would add cost and failure modes while changing nothing a reader can see.

Move only when one of these becomes true:

| Trigger | Where |
| --- | --- |
| Runs needed more than a few times a day | €4/month VPS, systemd timer |
| Repository goes private, so Actions minutes are metered | The same VPS |
| The bot must be always-on and off the laptop | Fly.io free tier, or the VPS |
| `data/` outgrows git — about 90 MB a year, so years away | Turso or Postgres, keep the NDJSON export |
| A real API is needed rather than a static site | Then FastAPI earns its place; it was cut for good reason |

The only one worth considering within months is moving the **bot**, which currently stops
answering whenever the laptop sleeps.

---

## Context that is not obvious

**The free tier's real limit is 200,000 tokens per day**, not the per-minute figure the
headers advertise. One full run costs roughly 40,000, so the day supports about five runs:
the cron, plus a few `/refresh` calls or one evaluation. Plan the day's model work around
that. A 429 whose message names a daily limit now stops the run immediately instead of
sleeping — see `DailyQuotaExceededError`.

**Actions owns `data/`.** Both runtimes can write it and they have already collided once in
a rebase. Local runs are for development; do not commit their output. If a conflict
appears, take the Actions version.

**A quiet run must never overwrite a good briefing.** `write_briefing` refuses to replace a
briefing with an empty one and returns `None`, and the pipeline then skips delivery. If you
change that path, keep both halves.

**Credentials** live in `.env`, which is gitignored, and in GitHub Actions secrets. Never
log a Telegram API URL — the token is in the path, and `httpx` was writing it to disk at
INFO level until that was fixed.

**The style of the codebase**: deterministic code does the work, the model is asked only
for judgement it is genuinely better at, and every model response is schema-validated. If a
change puts a model in a place where arithmetic would do, it is going the wrong way.

---

## Ask before doing

- Pushing anything that changes what the public site shows.
- Adding a source to `config/sources.yaml` — verify it with `scripts/verify_sources.py`
  first, and check it carries real publication dates or it will bypass the recency window.
- Anything that opens the bot to people other than the owner. That is the V5 multi-user
  path and it makes the project worse as a portfolio piece, not better.
