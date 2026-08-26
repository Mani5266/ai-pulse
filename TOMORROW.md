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

## Then, if there is time

Ordered by what improves the project most.

1. **Clustering recall.** The known weakness, documented in `PLAN.md` §2.9. Two outlets
   describing one event in different words stay two events unless they name the same model
   version. `EventPair` in `app/llm/schemas.py` and `event_pair_prompt` in
   `app/llm/prompts.py` exist for this and are unused: shortlist candidate pairs
   deterministically, then ask the model to adjudicate a handful. Budget is the constraint.
2. **The Rundown feed 403s intermittently** under bot protection. The retry usually catches
   it. If `stats.html` shows it failing most days, disable it in `config/sources.yaml` with
   a note, the way the other dead feeds were handled.
3. **A second briefing depth.** `PLAN.md` §40 of the original brief wanted a five-minute
   version alongside the sixty-second one. All the data exists; it is a second renderer.
4. **`docs/ARCHITECTURE.md` and `docs/SECURITY.md`.** Referenced in the project layout and
   never written. Most of the content is already in `PLAN.md` §2 and could be lifted.

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
