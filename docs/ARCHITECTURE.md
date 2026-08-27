# Architecture

How AI-Pulse turns roughly five hundred RSS articles a day into a five-story briefing, and
why each stage is where it is.

The organising principle is stated once here and holds everywhere below: **deterministic
code does the work, and the model is asked only for judgement it is genuinely better at.**
That is not an aesthetic preference. A free API tier has a token allowance measured in a
few hundred thousand tokens a day, and sending five hundred articles to a model would spend
it before the first briefing was written. Cutting five hundred down to twenty in ordinary
Python is what makes the whole project cost nothing.

`PLAN.md` §2 holds the decision record — including the decisions that were wrong first and
what replaced them. This document describes the system as it stands.

---

## The pipeline

```
   RSS feeds (~25 active, config/sources.yaml)
        |
   [ fetch ]        SSRF validation per hop, timeouts, 5 MB cap, 3 redirects
        |
   [ normalize ]    URL canonicalization, content hash
        |
   [ dedupe ]       URL hash, content hash, title trigram similarity
        |
   [ recency ]      window anchored on the last briefing, not on the clock
        |
   [ cluster ]      articles -> events (trigram overlap + shared entities)
        |
   [ score:code ]   credibility, novelty, personal relevance -> shortlist of 20
        |
   [ score:llm ]    technical / industry / developer impact, schema-validated
        |
   [ verify ]       claims cross-checked within the event cluster
        |
   [ rank ]         weighted score over all six sub-scores
        |
   [ edit ]         top 5 events -> briefing
        |
   +----------------+----------------+
   |                                 |
GitHub Pages                     Telegram
(public, permanent)              (private)
```

Everything above `[ score:llm ]` runs without a model and is unit-tested without one. That
is the line worth remembering when changing anything: if a change puts a model where
arithmetic would do, it is going the wrong way.

---

## Stage by stage

### Ingestion — `app/ingestion/`

`sources.py` reads the feed registry, `fetcher.py` performs every HTTP request in the
project through a single `SafeFetcher`, `feeds.py` parses with `feedparser`, and
`normalize.py` with `canonical.py` reduces a feed entry to a canonical article.

Feed URLs come from a registry file that the repository controls, but **redirects come from
the open internet**, so the fetcher validates every hop rather than only the first. The
security properties of this layer are the subject of [SECURITY.md](SECURITY.md). The
architectural point is narrower: there is exactly one way out to the network, so there is
exactly one place to enforce a limit.

Canonicalization strips tracking parameters and normalises the host and path, so the same
article arriving from two aggregators hashes identically.

### Deduplication — `app/ingestion/dedup.py`

Three passes, cheapest first: URL hash, then content hash, then title trigram similarity
above `0.90`. The memory is seven days, so an article resurfacing in a feed a week later is
still recognised.

Title similarity alone was not safe. Two different model releases from one lab produce
titles that differ by a version number and little else, which trigram similarity happily
calls the same story. The fix was an identity guard: near-identical titles that name
different versions or different models are kept apart. `PLAN.md` §2.8 has the case that
forced it.

### Recency — `app/ingestion/recency.py`

An RSS feed returns its *current window*, which is a day for an active news site and a year
for a quiet engineering blog. Filtering on "what the feed gave us" is therefore not a filter
at all: the first run of this pipeline ingested 517 articles spanning thirteen months and
published a "daily briefing" containing releases from April.

So the window is anchored on `last_briefing_at` in `data/state.json`, never on a fixed 24
hours. A run that is delayed, skipped, or run twice still covers exactly what has not been
reported. The catch-up is clamped, because "everything since" after a two-week gap is
thousands of articles and a briefing nobody reads.

This is also why a skipped scheduled run is survivable: the next run covers what the missed
one would have.

### Clustering — `app/intelligence/clustering.py`

Articles become events. Two articles join the same event when trigram overlap and shared
named entities clear a threshold of `0.45`.

**This is tuned for precision and it under-clusters.** Two outlets describing one event in
entirely different words stay two events unless they name the same model, lab, or version.
That is a deliberate trade: a false merge puts two unrelated stories under one headline and
is visible to the reader, while a missed merge costs a slot in the briefing and is not.
`PLAN.md` §2.9 documents the recall this gives up. `EventPair` in `app/llm/schemas.py`
exists to close the gap by asking the model to adjudicate a shortlist of candidate pairs,
and is deliberately unused: the budget is the constraint.

### Deterministic scoring — `app/ranking/`

Half the importance formula is computed in ordinary Python before any model is involved.

| Sub-score | Weight | Source |
| --- | --- | --- |
| `credibility` | 0.15 | Source registry, plus corroboration across sources |
| `novelty` | 0.15 | Event history — is this new, or the same story again? |
| `personal_relevance` | 0.15 | The profile in `config/profile.yaml` |
| `technical_impact` | 0.20 | Model |
| `industry_impact` | 0.15 | Model |
| `developer_impact` | 0.20 | Model |

The original design had the model produce five of the six and called the result
deterministic. A weighted average of model guesses is not deterministic. Splitting the
formula this way makes half of it reproducible and, more importantly, lets the
deterministic half cut roughly five hundred events to twenty **before the first model
call**. `shortlist.py` caps each category at four, so one busy category cannot take the
whole shortlist.

### The model layer — `app/llm/`

`provider.py` holds the provider abstraction and the OpenAI-compatible client that Groq,
Cerebras and OpenRouter all speak. `chain.py` composes providers into a failover chain.
`prompts.py` is the trust boundary. `schemas.py` holds the Pydantic models every response
must satisfy. `analysis.py` drives the calls.

Three rules govern this layer.

**The model gets data, never authority.** It receives structured text and returns structured
JSON. It has no shell, no filesystem, no network, no database write. Nothing it returns is
executed, and nothing it returns decides what the pipeline does next — only what a number
is.

**Every response is schema-validated.** A response that does not fit its schema is
discarded, not parsed leniently. One retry, then the event is marked `llm_failed` and the
run continues on the deterministic half of its score.

**The budget is quota and wall-clock, not money.** Everything here is free, so the limits
that bind are the daily token allowance and the runtime of a GitHub Actions job.
`llm_call_budget` caps calls per run at 60; `llm_timeout` caps a single call at 120 seconds.

### The provider chain — `app/llm/chain.py`

`AI_PULSE_LLM_CHAIN` names an ordered list of free tiers — `groq,openrouter` in production.
Each tier that has a key becomes a provider; a tier with no key is skipped, so an unset key
costs nothing.

**A chain advances only when a link has nothing left to give.** Two conditions qualify:

- `DailyQuotaExceededError` — the tier's allowance for the day is spent.
- `ProviderUnusableError` — HTTP 401, 402 or 403. The account cannot serve this run at all.

Nothing else advances the chain. Malformed JSON, a schema violation, or a timeout means the
*task* is hard, not that the *provider* is finished, and the next provider would fail the
same way while burning a second free allowance. Both advancing conditions set one flag,
`quota_exhausted`, which is the single thing that means "this link is done".

`ProviderUnusableError` exists because of a real failure. A Cerebras key listed models
successfully and then returned `402 Payment Required` on chat completions. Sitting second in
the chain, it fell into the generic error path, which returns `None` without setting the
flag — and a `None` without the flag reads as "the next provider fails the same way",
ending every run that got that far. A key that looks valid is not proof that a tier is
usable.

The chain is proven rather than assumed. From the CI log of 26 August:

```
groq:openai/gpt-oss-120b: daily allowance spent; no further calls
daily allowance spent on groq:openai/gpt-oss-120b; switching to openrouter:...
analysis complete provider=chain(groq:..., openrouter:...) scored=20 summarised=5
```

Groq was spent on arrival, OpenRouter carried 25 of 26 calls, and the briefing was
delivered. Before the chain existed, that run would have published with no prose at all.

### Verification — `app/intelligence/verification.py`

Claims are extracted from an event's articles and cross-checked *within the cluster*. A
claim two independent sources make is marked corroborated; a claim only one source makes is
still published, but it is not dressed up as established.

Corroboration turned out rarer than the design assumed — `PLAN.md` §2.14 — because the
clustering under-clusters, and a claim cannot corroborate across an event boundary that was
never crossed.

### Briefing and delivery — `app/briefing/`, `app/delivery/`

`builder.py` selects the top five and assembles the briefing; `render_html.py` and
`render_telegram.py` render it. Rendering happens **once, from data**, and never pads a gap:
if there are three stories worth publishing, the briefing has three stories.

A quiet run must never overwrite a good briefing. `write_briefing` refuses to replace an
existing briefing with an empty one and returns `None`, and the pipeline then skips delivery
rather than sending an empty message. If you change that path, keep both halves.

Delivery order is deliberate: **the briefing is persisted before it is sent.** Telegram is
the one stage that depends on somebody else's server, so a failure there must cost nothing.
The briefing is already on disk and already on the site, and the next run retries the send.

### Storage — `app/storage/`

NDJSON, one JSON object per line, partitioned by UTC date, committed to git:

```
data/articles/2026-08-27.ndjson
data/events/2026-08-27.ndjson
data/briefings/2026-08-27.json
data/runs/2026-08.ndjson
data/state.json
```

Git is the database. A committed SQLite binary would bloat the repository and diff as noise;
line-oriented JSON appends cleanly and diffs as added lines, so the git history of `data/`
*is* the intelligence timeline the product promises. Keys are written sorted, so a re-run
that changes nothing produces no diff.

At roughly 90 MB a year this outgrows git in several years, not months. `TODO.md` carries
the trigger for moving and the destination.

**Actions owns `data/`.** Both the workflow and a local run can write it, and they have
already collided once in a rebase. Local runs are for development: do not commit their
output, and if a conflict appears, take the Actions version.

---

## Runtime

There is no server. Four workflows in `.github/workflows/` are the whole deployment.

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | push, pull request | ruff, ruff format, mypy strict, pytest, `pip-audit`, the evaluation gate |
| `daily.yml` | 02:00 UTC, or dispatch | Runs the pipeline, commits `data/`, deploys Pages |
| `bot.yml` | every 5 minutes, or dispatch | One `drain()` pass over pending Telegram updates |
| `feeds.yml` | Mondays 03:00 UTC | Verifies every feed still parses, so a dead source surfaces |

`app/jobs/` holds the three entry points: `daily_briefing.py` for the pipeline,
`poll_bot.py` for the one-shot drain the cron uses, and `serve_bot.py` for a long-running
process where one is available.

**A scheduled workflow is not a process, and GitHub does not pretend otherwise.** Runs are
dropped under load without notice: on 26 August a `*/5` bot schedule landed three times in
six hours, and the 02:00 run on the 27th was skipped entirely. The pipeline tolerates this
because the recency window follows the last briefing rather than the clock. A person waiting
for a bot reply does not, which is the standing argument for moving only the bot to a real
process.

---

## Configuration

`app/core/config.py` is a single pydantic-settings `Settings` class. Every value has a
default, a validated range, and an `AI_PULSE_`-prefixed environment variable.
`.env.example` lists all of them. `.env` is gitignored and is never committed.

Two conventions are worth knowing:

- An empty environment variable is treated as unset. `.env.example` ships its keys blank,
  and a blank value must not be mistaken for a configured one.
- A per-tier key such as `AI_PULSE_GROQ_API_KEY` takes precedence over the generic
  `AI_PULSE_LLM_API_KEY`, which remains supported for a single-provider deployment.

---

## Failure policy

| Failure | Response |
| --- | --- |
| One feed fails | Log it, record it in the run stats, continue with the rest |
| A model response fails validation | Retry once, then mark the event `llm_failed` and continue |
| A provider is out of quota or unusable | Advance to the next link in the chain |
| Every provider is gone | Publish on the deterministic ranking, without prose |
| Telegram delivery fails | The briefing is already persisted; the next run retries |
| The pipeline crashes | `state.json` advances only on success, so the next run re-covers the window |

Every stage is restartable, and the worst case is a briefing that is less good rather than a
day that is lost.

The failure this design cannot see is the quiet one: a run that *succeeds* while publishing
two stories instead of five. GitHub emails on a failed workflow and says nothing about a
thin one. `PLAN.md` §2.14 states the problem; a threshold alert is on the list in `TODO.md`.

---

## Testing

Tests live in `tests/{unit,integration,security}/` and run without network access, without a
model, and without credentials. Everything that touches the outside world takes an
injectable client or resolver, which is why the SSRF guard can be tested against a fake
resolver and the fetcher against a mock transport.

The suite is not the only gate. CI also runs `pip-audit` against pinned lockfiles and
`scripts/eval.py`, which fails the build if the structural injection corpus reports an
escape.
