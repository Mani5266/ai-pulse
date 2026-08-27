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

**A model API key.** [console.groq.com/keys](https://console.groq.com/keys) — free tier, no
card. The free allowance is 200,000 tokens a day; one run costs roughly 40,000, so a daily
briefing plus a few manual runs fits comfortably.

**A Telegram bot,** if you want the briefing on your phone. Message
[@BotFather](https://t.me/BotFather), send `/newbot`, and keep the token. Then message your
new bot once — a bot cannot start a conversation, so it needs one inbound message before it
can reply.

Delivery is optional. Without it the pipeline still runs and still publishes the site.

### 2. Fork and add the secrets

Fork the repository, then under **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
| --- | --- |
| `AI_PULSE_LLM_API_KEY` | your Groq key |
| `AI_PULSE_TELEGRAM_BOT_TOKEN` | your bot token, if using Telegram |
| `AI_PULSE_TELEGRAM_CHAT_ID` | your chat id, if using Telegram |

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
   the LLM, a nominal run spends about 30 calls, and the run is capped at 60, so the
   pipeline fits inside any free API tier.
2. **The LLM receives data, never authority.** No shell, no filesystem, no browser, no
   database write, no network. Article text is wrapped in `<document>` tags and the system
   prompt declares it untrusted. Every response is validated against a Pydantic schema.
3. **Git is the database.** Article and event records are committed as NDJSON, so the
   repository history *is* the timeline. A SQLite database is rebuilt from it on demand
   and is gitignored.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks the pipeline stage by stage, and
[docs/SECURITY.md](docs/SECURITY.md) covers the threat model, the SSRF guard and the
prompt-injection boundary — including what they do not defend against. Full reasoning,
including the rejected alternatives, is in [PLAN.md](PLAN.md).

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
| Test coverage | **93%** of `app/`, branch coverage, floor of 88% enforced in CI |
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

Python 3.11 · httpx · feedparser · Pydantic · Ruff · MyPy (strict) · pytest ·
GitHub Actions · GitHub Pages · Telegram Bot API · Groq free tier (CI) ·
Ollama (local development)

Recurring cost: zero.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| P0 | Repository, tooling, CI | Done |
| P1 | Feed ingestion with SSRF protection | Done — 22/22 feeds live, 515 articles/run |
| P2 | Canonicalization and deduplication | Done — 3-pass, deterministic |
| P3 | Event clustering | Done — precision-tuned, under-clusters |
| P4 | Deterministic scoring | Done — 490 events cut to 20 |
| P5 | LLM provider layer | Done — schema-validated, budget-capped |
| P6 | Briefing, Telegram, Pages | Done — delivered, site builds |
| P7 | Claim verification | Done — labels computed in code |
| P8 | Timeline | Done — built from committed snapshots |
| P9 | Evaluation harness | Harness done; labels outstanding |
| P10 | Observability | Done — published at /stats.html |

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

## Running it unattended

Two Windows scheduled tasks, registered against the virtualenv's interpreter directly
rather than through a shell wrapper — a wrapped command gets a console, and that console
receives a CTRL+C when the session that started it goes away, which killed the first
scheduled run mid-flight. Logging therefore happens inside the process, to
`AI_PULSE_LOG_FILE`.

| Task | Trigger | What it does |
| --- | --- | --- |
| `AI-Pulse Bot` | At logon, restarts on failure | Long-polls Telegram and answers |
| `AI-Pulse Daily Briefing` | 07:30 daily, catches up if missed | Builds and delivers |

```powershell
Get-ScheduledTask -TaskName "AI-Pulse*"
Start-ScheduledTask -TaskName "AI-Pulse Daily Briefing"   # run one now
Get-Content data\ai-pulse.log -Tail 20
```

`StartWhenAvailable` is what makes a missed 07:30 run at the next opportunity instead of
being skipped, which together with windowing on the last briefing means a machine that was
asleep loses nothing.

## License

MIT. See [LICENSE](LICENSE).
