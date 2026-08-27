# AI-Pulse

[![CI](https://github.com/Mani5266/ai-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/Mani5266/ai-pulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A multi-source event intelligence pipeline for AI news. It ingests ~500 articles a day
from ~25 RSS feeds, deduplicates them, clusters them into distinct **events**,
cross-checks each event's claims against independent sources, ranks them deterministically,
and publishes a short evidence-backed briefing.

## Run your own

Fork this repository and it runs on your GitHub Actions, your API quota, your Telegram.
Nothing is shared with this instance. Recurring cost is zero, and the whole thing is about
ten minutes.

### 1. Get two credentials

**Model API keys — one is enough, two is better.** Both free, neither needs a card:

- [console.groq.com/keys](https://console.groq.com/keys) — 200,000 tokens a day. One run
  costs roughly 40,000.
- [openrouter.ai/keys](https://openrouter.ai/keys) — a second free allowance.

The run uses them as a chain. When Groq's allowance is spent mid-run, the next call goes to
OpenRouter and the briefing finishes instead of degrading to a headline list. That is not
theoretical: it happened on the first day this shipped, and it is also what let the
prompt-injection corpus run at full coverage — one tier ran out after four attacks.

**A Telegram bot,** if you want the briefing on your phone. Message
[@BotFather](https://t.me/BotFather), send `/newbot`, and keep the token. Then message your
new bot once — a bot cannot start a conversation, so it needs one inbound message before it
can reply.

Delivery is optional. Without it the pipeline still runs and still publishes the site.

### 2. Fork and add the secrets

Fork the repository, then under **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
| --- | --- |
| `AI_PULSE_GROQ_API_KEY` | your Groq key |
| `AI_PULSE_OPENROUTER_API_KEY` | your OpenRouter key — optional, and the difference between a briefing that finishes and one that stops halfway |
| `AI_PULSE_TELEGRAM_BOT_TOKEN` | your bot token, if using Telegram |
| `AI_PULSE_TELEGRAM_CHAT_ID` | your chat id, if using Telegram |

A tier with no key is skipped, so an unset one costs nothing. `AI_PULSE_LLM_API_KEY` still
works for a single-provider deployment, but the per-tier names are what the workflow reads
first.

To find your chat id, message your bot and then open
`https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` — it is the `chat.id` field.

### 3. Turn on Pages and run it

Under **Settings → Pages**, set the source to **GitHub Actions**. Then go to
**Actions → Daily briefing → Run workflow**.

The first run takes two or three minutes. It publishes to
`https://<your-username>.github.io/<repo>/` and, if you configured Telegram, sends the
briefing to your phone. After that it runs itself at 02:00 UTC daily — edit the `cron` line
in `.github/workflows/daily.yml` to change the time.

### 4. Make it yours

Two files decide what you read:

**`config/profile.yaml`** — your interests, the things you would rather not read about, and
how much each category is worth. This is the only file that encodes taste rather than
engineering, and editing it changes the ranking immediately. Start here.

**`config/sources.yaml`** — the feeds. Add or remove sources freely, then verify them:

```bash
python scripts/verify_sources.py
```

Feeds die quietly, so check any you add. A feed without real publication dates will bypass
the recency window and fill your briefing with old news — the verifier prints the newest
item's date so you can tell.

### Running it locally instead

```bash
git clone https://github.com/<you>/ai-pulse && cd ai-pulse
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
cp .env.example .env                              # then fill it in
python -m app.jobs.daily_briefing
```

To run the model locally instead of through an API, install [Ollama](https://ollama.com),
`ollama pull qwen3:4b`, and set `AI_PULSE_LLM_PROVIDER=ollama`. A 4B model fits in 4 GB of
VRAM and takes about six seconds a call with thinking disabled, which is the default — see
`AI_PULSE_OLLAMA_THINK` and the note beside it for why.

`python -m app.jobs.serve_bot` runs the Telegram bot locally, answering `/latest`,
`/refresh` and `/status` by long polling.

The deployed bot does not run this. It is a Cloudflare Worker on a webhook — free, always
on, and replying in about a second — and it contains none of the project's text: the daily
run renders every reply it can give and publishes them as `bot.json` alongside the site,
and the Worker picks one by command. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
`worker/`.

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

One principle decides everything below it: **deterministic code does the work, and the
model is asked only for judgement it is genuinely better at.** That is not taste. A free
tier allows 200,000 tokens a day, and sending five hundred articles to a model would spend
it before the first briefing was written. Cutting five hundred to twenty in ordinary Python
is what makes the whole thing cost nothing.

```
   RSS feeds (~25)
        |
   [ fetch ]        SSRF validation per hop, 5 MB cap, 3 redirects, timeouts
        |
   [ normalize ]    URL canonicalization, content hash
        |
   [ dedupe ]       URL hash, content hash, title trigram similarity
        |
   [ recency ]      window follows the last briefing; nothing older than 24h
        |
   [ cluster ]      articles -> events (trigram overlap + shared entities)
        |
   [ score:code ]   credibility, novelty, personal relevance -> top 20    <- 500 to 20 here
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
(public, permanent)              (private, ~1s)
```

Everything above `[ score:llm ]` runs without a model and is unit-tested without one.

### The six-part score, and which half the model touches

| Sub-score | Weight | Source |
| --- | --- | --- |
| `credibility` | 0.15 | Source registry, plus corroboration across sources |
| `novelty` | 0.15 | Event history — is this new, or the same story again? |
| `personal_relevance` | 0.15 | `config/profile.yaml` |
| `technical_impact` | 0.20 | Model |
| `industry_impact` | 0.15 | Model |
| `developer_impact` | 0.20 | Model |

The first design had the model produce five of the six and called the result
deterministic. A weighted average of model guesses is not deterministic. Splitting it this
way makes half the formula reproducible and lets the deterministic half do the cutting
**before the first model call**.

### Three rules for the model

**It gets data, never authority.** Structured text in, structured JSON out. No shell, no
filesystem, no network, no database write. Nothing it returns is executed, and nothing it
returns decides what the pipeline does next — only what a number is. A test asserts the
provider class exposes no method named `run`, `execute`, `shell`, `read_file`, `fetch` or
`tools`, so the property cannot be lost by accident.

**Every response is schema-validated.** One that does not fit is discarded, not parsed
leniently. One retry, then the event keeps only its deterministic score and the run
continues.

**The budget is quota and wall-clock, not money.** 60 calls per run, 120 seconds per call.
A nominal run spends about 30.

### The provider chain

`AI_PULSE_LLM_CHAIN` names free tiers in order — `groq,openrouter`. A chain advances only
when a link has nothing left to give: a spent daily allowance, or HTTP 401/402/403. Never
on malformed JSON, a schema violation, or a timeout, because those mean the *task* is hard
rather than the *provider* finished, and moving on would burn a second free allowance on
the same failure.

That distinction came from a real defect. A Cerebras key listed models happily and returned
`402 Payment Required` on every completion. Sitting second in the chain, it fell into the
generic error path — which returns nothing without marking the provider spent, and a
chain reads that as "the next one fails the same way" and stops. It would have ended every
run that reached it.

### Git is the database

NDJSON, one object per line, partitioned by UTC date, committed:

```
data/articles/2026-08-27.ndjson    data/briefings/2026-08-27.json
data/events/2026-08-27.ndjson      data/runs/2026-08.ndjson
```

A committed SQLite binary would bloat the repository and diff as noise. Line-oriented JSON
appends cleanly and diffs as added lines, so the git history of `data/` *is* the timeline
the product promises. Keys are written sorted, so a re-run that changes nothing produces no
diff.

### Where it runs

No server, and nothing on a laptop.

| Where | What | When |
| --- | --- | --- |
| GitHub Actions | The pipeline; commits `data/`, deploys Pages | 02:00 UTC, retried 08:00 if skipped |
| GitHub Pages | The site, and `bot.json` — every reply the bot can give | Deployed by the run above |
| Cloudflare Workers | The Telegram webhook | Per message, ~1 second |

The bot is a webhook rather than a poller because GitHub throttles scheduled runs — a `*/5`
cron landed three times in six hours. The Worker holds **no project text**: the daily run
renders every reply it can give into `bot.json`, and the Worker picks one by command. Two
renderers would drift; this way there is one, in Python, under test.

### When something breaks

| Failure | Response |
| --- | --- |
| One feed fails | Logged, recorded in run stats, run continues |
| A response fails validation | Retry once, mark the event `llm_failed`, continue |
| A provider is spent or unusable | Advance to the next link in the chain |
| Every provider is gone | Publish on the deterministic ranking, without prose |
| Telegram delivery fails | The briefing is already saved; the next run retries |
| The pipeline crashes | `state.json` advances only on success, so the next run re-covers the window |

The failure this design could not see was the *quiet* one — a run that succeeds while
publishing two stories instead of five. GitHub emails on a failed workflow and says nothing
about a thin one. `app/delivery/health.py` closes that: it reads the run record and sends a
Telegram notice when the run was degraded. Quiet is not degraded, and the distinction is
the whole module — two stories from a shortlist of two is the pipeline working; two from a
shortlist of twenty is a fault.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks every stage and the constraint that put
it there. [docs/SECURITY.md](docs/SECURITY.md) covers the threat model, the SSRF guard and
the prompt-injection boundary — including what they do not defend against. `PLAN.md` §2 is
the decision record, including the decisions that were wrong first.


## Measured

Run `python scripts/eval.py` to reproduce. Structural checks are offline and free, so CI
runs them on every push; the model layer needs a key and a quota and is run deliberately
with `--with-model`.

| | |
| --- | --- |
| Injection corpus, structural | **0 escapes of 40** — no payload leaves its document |
| Injection corpus, model layer | **0 escapes of 40** — full coverage, run 2026-08-27 against the production chain |
| Stories citing a source | **100%** |
| Claim attributions valid | **100%** — an attribution to a source the event lacks is discarded |
| Duplicate events in one briefing | **0** |
| Test coverage | **94%** of `app/`, branch coverage, floor of 88% enforced in CI |
| Tests | **651** Python, **24** JavaScript for the webhook |
| Precision, category accuracy | **pending labels** |

The last row is deliberately blank. Those metrics need a person to say whether a story
mattered, and a number the author graded against their own guess measures nothing. Run
`python scripts/eval.py --label-sheet`, fill in the importance column, save it as
`evals/dataset.json`, and they compute themselves.

An "escape" means the pipeline's output changed — a rejected response, scores pushed to
the ceiling, the system prompt leaking, a fabricated source surviving verification. A model
that reads an injected instruction, declines it, and returns a well-formed analysis has not
been compromised; counting that as a failure would produce an alarming number that means
nothing.

## Stack

Python 3.11 · httpx · feedparser · Pydantic · Ruff · MyPy (strict) · pytest · pytest-cov ·
GitHub Actions · GitHub Pages · Cloudflare Workers · Telegram Bot API ·
Groq and OpenRouter free tiers · Ollama (local development)

Recurring cost: zero. No card is required for any of it.

Deliberately absent: Docker, Kubernetes, Postgres, a message queue, a web framework. Each
was considered and cut — `PLAN.md` §31 is the argument. Infrastructure with no user reads
as cargo cult rather than as rigour, and a `Dockerfile` that nothing deployed was deleted
for exactly that reason.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| P0 | Repository, tooling, CI | Done |
| P1 | Feed ingestion with SSRF protection | Done — 24/25 feeds live, ~530 articles/run |
| P2 | Canonicalization and deduplication | Done — 3-pass, deterministic |
| P3 | Event clustering | Done — precision-tuned, under-clusters |
| P4 | Deterministic scoring | Done — ~500 events cut to 20 before the first model call |
| P5 | LLM provider layer | Done — schema-validated, budget-capped |
| P6 | Briefing, Telegram, Pages | Done — delivered, site builds |
| P7 | Claim verification | Done — labels computed in code |
| P8 | Timeline | Done — built from committed snapshots |
| P9 | Evaluation harness | Harness done; labels outstanding |
| P10 | Observability | Done — published at /stats.html |

Hardening, all done: pinned dependencies, `pip-audit` in CI, weekly feed verification, a
fallback model provider, architecture and security documents, a coverage floor, and
alerting on a degraded run.

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
pytest --cov                       # the coverage floor, as CI enforces it

cd worker && node --test           # the Telegram webhook
cd ..

python scripts/verify_sources.py   # check every feed is still alive
python -m app.jobs.daily_briefing
python scripts/publish_site.py     # re-render the site from committed data; no model
```

`--cov` is not in `addopts` on purpose: it would make running one test file fail a
whole-project floor, which trains people to pass `--no-cov` and defeats the point.

`.env` is gitignored and must never be committed. Only `.env.example` belongs in the
repository.

## Running it unattended

Nothing runs on a laptop. The pipeline is a GitHub Actions workflow, the site is Pages, and
the bot is a Cloudflare Worker on a webhook — none of which needs a machine of yours to be
awake, and all of which are free.

| Where | What runs | When |
| --- | --- | --- |
| GitHub Actions | The pipeline, committing `data/` and deploying Pages | 02:00 UTC, retried at 08:00 if the first was skipped |
| GitHub Pages | The site, and `bot.json` — every reply the bot can give | Deployed by the run above |
| Cloudflare Workers | The Telegram webhook | On each message, in about a second |

```bash
gh workflow run "Daily briefing"   # build one now
gh workflow run "Publish site"     # re-render from committed data; no model, no quota
gh run list --limit 5
```

This did run on Windows Task Scheduler first, and `PLAN.md` §2.1 keeps the reason it moved:
a scheduled task is only as reliable as the machine under it, and the first one died to a
CTRL+C its console received when the starting session went away. Two things outlived that
decision and are still here — logging happens inside the process rather than through a
shell redirect (`AI_PULSE_LOG_FILE`), and the recency window follows the last briefing
rather than the clock, so a skipped run costs a delay and nothing else.

## License

MIT. See [LICENSE](LICENSE).
