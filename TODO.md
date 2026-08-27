# What is left

Hand this to a fresh session. It carries only outstanding work, plus the context that is
not visible in the code.

P0–P10 are complete and the pipeline runs unattended. Every hardening item is done and
pushed: pinned dependencies, dependency auditing, weekly feed verification, a fallback
model provider, the architecture and security documents, a coverage floor, and
degraded-run alerting.

| | |
| --- | --- |
| Live site | https://mani5266.github.io/ai-pulse/ |
| Repository | https://github.com/Mani5266/ai-pulse (public) |
| Telegram bot | @Mani_aipulse_bot — send anything for the briefing; `/status` and `/help` |
| Bot runtime | Cloudflare Worker on a webhook, https://ai-pulse-bot.mani5266.workers.dev — replies in about a second, free |
| Daily run | GitHub Actions, 02:00 UTC (07:30 IST), commits `data/` back |
| Tests | 642 passing, 94% branch coverage; ruff, mypy strict, pip-audit and the structural evaluation gate CI |

The `Bot` workflow is **disabled on purpose**. Telegram allows one consumer per token and
the webhook is it; re-enabling the schedule would make two processes fight over the queue.
Both Windows scheduled tasks have been removed for the same reason: nothing in this
project now depends on one machine being awake.

`PLAN.md` §2 holds every design decision, including the ones that were wrong first. Read it
before changing the pipeline — most of those sections exist because something failed in a
way that was not obvious.

---

## Blocked, not forgotten

One thing is waiting on something external rather than on a decision.

The injection corpus is no longer among them. It ran at full coverage on 27 August 2026 —
40 of 40 attacks against the model, zero escapes — which is what the provider chain was
built for: a single free tier ran out after three or four attacks, and two tiers finished
the corpus. Re-run it after any change to `app/llm/prompts.py`, early in the day:

```bash
python scripts/eval.py --with-model
```

If a future report says "only N% of the corpus reached it", report that rather than the
escape count. A number earned at partial coverage is not the number.

**Label events, for precision and category accuracy.** Both are blank by design. They need
a person to say whether a story mattered, and that person is the repository owner: the
pipeline already ranks using `config/profile.yaml`, so grading against it would measure the
profile rather than the pipeline.

```bash
python scripts/eval.py --label-sheet > evals/dataset.json
```

Then set `importance` on each row to `important`, `marginal` or `noise`, and correct
`category` only where the pipeline got it wrong. Worth doing once several days of briefings
exist. **Do not fill these in on the owner's behalf.**

---

## Hardening, remaining

All seven are done.

---

## Product work, if there is time

1. **Clustering recall.** The known weakness, documented in `PLAN.md` §2.9. Two outlets
   describing one event in different words stay two events unless they name the same model
   version. `EventPair` in `app/llm/schemas.py` and `event_pair_prompt` in
   `app/llm/prompts.py` exist for this and are unused: shortlist candidate pairs
   deterministically, then ask the model to adjudicate a handful. Budget is the constraint.
2. **The Rundown feed 403s intermittently** under bot protection. The retry usually catches
   it, and the weekly feed workflow will now surface it if it becomes persistent. If
   `stats.html` shows it failing most days, disable it in `config/sources.yaml` with a note,
   the way the other dead feeds were handled.
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
| `data/` outgrows git — about 90 MB a year, so years away | Turso or Postgres, keep the NDJSON export |
| A real API is needed rather than a static site | Then FastAPI earns its place; it was cut for good reason |

None of these is close, and the one that was is now solved. The bot used to answer on a
`*/5` schedule that GitHub throttled to three runs in six hours, so a message could wait
until morning. It is a Cloudflare Worker on a webhook now: about a second, free, and no
machine of ours involved. The container that was drafted to fix it — `Dockerfile`,
`fly.toml` — has been removed rather than kept warm. It cost roughly $2 a month, this
project's premise is zero cost, and infrastructure nothing deploys is the cargo cult named
two paragraphs below.

**Deliberately not on any list:** Docker, Kubernetes, Postgres, a message queue, a FastAPI
layer, multi-region anything. Each is a moving part with no user. `PLAN.md` §31 is the
argument, and unused infrastructure reads as cargo cult rather than as rigour.

---

## Context that is not obvious

**The free tier's real limit is 200,000 tokens per day**, not the per-minute figure the
headers advertise. One full run costs roughly 40,000, so one tier supports about five runs a
day: the cron, plus a few `/refresh` calls or one evaluation. The chain buys a second
allowance rather than a larger one — when Groq is spent the run continues on OpenRouter, so
the day's capacity is roughly doubled, but each tier still stops dead at its own limit. Plan
the day's model work around that. A 429 whose message names a daily limit stops the run immediately instead of sleeping
— see `DailyQuotaExceededError`. The headers cannot be used to tell the two apart: a
per-day rejection still reports a per-minute limit and a full per-minute remainder.

**Actions owns `data/`.** Both runtimes can write it and they have already collided once in
a rebase. Local runs are for development; do not commit their output. If a conflict appears,
take the Actions version.

**A quiet run must never overwrite a good briefing.** `write_briefing` refuses to replace a
briefing with an empty one and returns `None`, and the pipeline then skips delivery. If you
change that path, keep both halves.

**Credentials** live in `.env`, which is gitignored, and in GitHub Actions secrets. Never
log a Telegram API URL — the token is in the path, and `httpx` was writing it to disk at
INFO level until that was fixed.

**Backslash escapes have caused four separate defects** in this project — a bell character
where `.venv\Scripts\activate` should be, a regex that matched nothing, and two mangled
anchors. Any scripted edit should assert its anchor is present before writing, and build
backslashes from an explicit character code rather than an escape.

**The style of the codebase**: deterministic code does the work, the model is asked only for
judgement it is genuinely better at, and every model response is schema-validated. If a
change puts a model where arithmetic would do, it is going the wrong way.

---

## Ask before doing

- Pushing anything that changes what the public site shows.
- Adding a source to `config/sources.yaml` — verify it with `scripts/verify_sources.py`
  first, and check it carries real publication dates or it will bypass the recency window.
- Anything that opens the bot to people other than the owner. That is the V5 multi-user
  path and it makes the project worse as a portfolio piece, not better.
