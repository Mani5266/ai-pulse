# AI-Pulse — Build Plan

**Status:** P1 complete
**Last updated:** 2026-08-26

---

## 1. What this is

AI-Pulse is a multi-source event intelligence pipeline for AI news. It ingests roughly
500 articles per day from ~25 RSS feeds, deduplicates them, clusters them into distinct
*events*, cross-checks the claims in each event against independent sources, ranks them,
and publishes a short evidence-backed briefing — to a public static site and to Telegram.

It is not a news summarizer. The distinction matters and is the reason the project is
worth building:

- A summarizer treats the **article** as the unit of work and produces prose.
- AI-Pulse treats the **event** as the unit of work, tracks how each event develops
  across days, and attaches a verification status to every material claim.

### The pitch (use this wording)

> Multi-source event intelligence pipeline — deduplicates ~500 articles/day into ~5
> verified events, cross-checks claims against independent sources, and tracks how
> stories develop over time. Every LLM response is schema-validated; the injection
> corpus contains 40 attacks with 0 escapes.

### Non-goals

- Not a SaaS product. Single user.
- Not multi-tenant. No authentication, no billing, no team workspaces.
- Not a general chatbot. The LLM has no tools, no shell, no filesystem, no network.

---

## 2. Design decisions and their rationale

These are the decisions worth defending in an interview. Each one is a place where the
obvious choice was rejected for a reason.

### 2.1 GitHub Actions is the runtime, not a laptop

The original design ran the pipeline on a local machine via Windows Task Scheduler with
a local Ollama model. That was rejected for two reasons:

1. **Reliability.** The stated target is >=95% daily success. A laptop is asleep at
   07:00. A hosted cron is not.
2. **Demonstrability.** A project that only runs on one machine has no public URL, so
   there is nothing for a reader to look at.

The runtime is therefore:

```
GitHub Actions cron (free on public repos)
        |
   Python pipeline
        |
   free-tier LLM API
        |
   NDJSON committed back to the repo (git is the database)
        |
   static site build
        |
   GitHub Pages (free, permanent URL)  +  Telegram (private delivery)
```

No server, no card, no paid tier, and the git history of `data/` is itself a
demonstration of the timeline feature.

**Caveat:** GitHub's scheduled workflows can be delayed under load. The pipeline must
therefore window on `last_briefing_at` (persisted state), never on "the last 24 hours".

### 2.2 Two LLM providers, chosen by environment

Ollama cannot run on a standard GitHub-hosted runner. Rather than abandon local
inference, `LLMProvider` has two implementations:

| Environment | Provider | Why |
| --- | --- | --- |
| CI / production | Free hosted API tier | Runs where the cron runs |
| Local development | Ollama | No quota, no network, fast iteration |

This is the reason the abstraction exists. It is not speculative generality.

Free tiers change their terms without notice. The design therefore budgets **<=25 LLM
calls per pipeline run**, which fits comfortably inside every candidate free tier
(Gemini, Groq, Cerebras, OpenRouter free models). Verify current quotas before
committing to one; do not hard-code a provider.

### 2.3 The database is NDJSON in git, not a committed SQLite binary

A committed `.db` file bloats the repository and produces unreadable diffs. Instead:

```
data/articles/2026-08-26.ndjson     # one JSON object per line, metadata only
data/events/2026-08-26.ndjson
data/briefings/2026-08-26.json
data/state.json                     # last_briefing_at, run counters
```

**Full article text is never committed.** Measured on a real run of 22 feeds: 515
articles with full text is 2.0 MB, which is ~730 MB of repository growth per year.
Deleting those files later would not help, because git history is immutable and the blobs
stay in it forever. So the persisted record carries identity, provenance, timing and a
500-character summary — enough for a later day to recognise the story again — and full
text lives only in memory, during the run, which is when clustering and the LLM need it.

Measured after that change: 515 articles is 252 KB, about 500 bytes per record, or
roughly 90 MB per year. Event records are retained permanently. A SQLite database is
rebuilt from the NDJSON on demand for local analysis, and is gitignored.

### 2.4 Deterministic code first, LLM only where reasoning is required

The original scoring formula was described as deterministic, but five of its six
sub-scores came from the model. Weighted averages of model guesses are not
deterministic. The split is now explicit:

| Sub-score | Computed by | How |
| --- | --- | --- |
| `credibility` | **Code** | Lookup in the source registry |
| `novelty` | **Code** | History check — is this entity or event new? |
| `personal_relevance` | **Code** | Keyword match against the interests config |
| `technical_impact` | LLM | Requires judgement |
| `industry_impact` | LLM | Requires judgement |
| `developer_impact` | LLM | Requires judgement |

```
importance_score =
    technical_impact    * 0.20
  + industry_impact     * 0.15
  + developer_impact    * 0.20
  + novelty             * 0.15
  + credibility         * 0.15
  + personal_relevance  * 0.15
```

Weights sum to 1.00. Half the inputs are now reproducible without a model, and the
LLM surface shrinks accordingly.

### 2.5 The LLM budget is wall-clock and quota, not money

A structured call against a 7B local model takes 20-60 seconds. Scoring 100 events would
take hours. The hard rule is therefore:

> Deterministic filtering reduces the candidate set to at most 20 events **before the
> first LLM call**.

Total LLM calls per run: <=25.

### 2.6 The LLM gets data, never authority

The summarizer needs no shell, no filesystem, no browser, no database write, and no
network. It receives structured article data and returns structured JSON. The
application performs every side effect.

Article text is wrapped in `<DOCUMENT>` tags and the system prompt states that document
content is untrusted data that must never be executed as instructions. Every response is
validated against a Pydantic model; a validation failure retries once, then marks the
event `llm_failed` and the pipeline continues.

### 2.7 Cut list

Removed from the original design, with reasons:

| Removed | Reason |
| --- | --- |
| FastAPI | No user calls it. The pipeline is a cron job. |
| Docker | GPU passthrough on Windows is painful; adds nothing for a single user. |
| Alembic | Migrations for a rebuildable derived database are overhead. |
| Embeddings / vector DB | Trigram + entity overlap is sufficient for 25 feeds. |
| PostgreSQL | Single user. |
| Feedback loop / personalization | Deferred until the core is stable for 30 days. |

Unused infrastructure is a liability, not a signal of sophistication.

### 2.8 arXiv needs its own quota

`cs.AI` and `cs.LG` together publish 300-600 papers per day and will drown every other
source. Research feeds get a separate daily cap and a keyword prefilter before entering
the main candidate pool.

---

## 3. Architecture

```
   RSS feeds (~25)
        |
   [ fetch ]        SSRF guard, timeouts, size caps, redirect limit
        |
   [ normalize ]    URL canonicalization, content hash
        |
   [ dedupe ]       URL hash, content hash, title trigram similarity
        |
   [ cluster ]      articles -> events (trigram + shared entities)
        |
   [ score:code ]   credibility, novelty, personal_relevance -> top 20
        |
   [ score:llm ]    technical/industry/developer impact
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
(public, permanent)              (private)
```

Everything above `[ score:llm ]` is deterministic and unit-testable without a model.

### Project layout

```
ai-pulse/
├── app/
│   ├── core/            config, logging, errors
│   ├── ingestion/       feed fetching, normalization, dedup
│   ├── intelligence/    clustering, claim extraction, verification
│   ├── llm/             provider abstraction, schemas, prompts
│   ├── ranking/         deterministic scoring
│   ├── briefing/        editor, renderers
│   ├── delivery/        telegram, static site
│   └── jobs/            daily_briefing entrypoint
├── tests/{unit,integration,security}/
├── evals/               dataset + injection corpus
├── data/                NDJSON, committed
├── docs/                ARCHITECTURE.md, SECURITY.md, DECISIONS.md
├── site/                generated static site
└── .github/workflows/   ci.yml, daily.yml
```

---

## 4. Phases

Each phase ships a concrete artifact. A phase that produces no artifact is
deprioritized.

### P0 — Repository and CI skeleton

Public repo, MIT licence, `pyproject.toml`, Ruff, MyPy in strict mode, pytest. A CI
workflow that runs lint, format check, type check and tests on every push and pull
request. Configuration loading with typed settings.

*Artifact: a green CI badge in the README from the first commit.*

### P1 — Ingestion

Fetch ~25 feeds. Verify each URL returns HTTP 200 and valid XML before adding it — expect
three to five of the candidate feeds to be dead (Reuters' RSS service has largely been
retired). Persist article records as NDJSON.

Security requirements, non-negotiable:

- Resolve each hostname and reject private, loopback, link-local and reserved IP ranges.
  Check the resolved address, not a regular expression on the hostname.
- Connect and read timeouts on every request.
- A maximum response size, enforced while streaming.
- A redirect limit, with the SSRF check re-applied after each redirect.

*Artifact: security tests covering each of the above.*

### P2 — Canonicalization and deduplication

`CanonicalURLService` (scheme, host, path, sorted query, tracking-parameter removal,
fragment removal), `HashService` (content hash), and title trigram similarity. No
embeddings, no model.

*Artifact: unit tests built from real duplicate article pairs.*

### P3 — Event clustering

Group articles into events using trigram similarity plus shared named entities. Each
event carries `event_id`, `canonical_title`, `first_seen`, `last_updated`,
`article_count`, `category`, `confidence`.

*Artifact: the core differentiator exists in code.*

### P4 — Deterministic scoring

Compute `credibility`, `novelty` and `personal_relevance` in plain Python. Reduce the
candidate set to the top 20 events before any model is invoked.

*Artifact: a reproducible ranking with tests.*

### P5 — LLM layer

`LLMProvider` interface with `generate()` and `structured_generate()`; hosted and Ollama
implementations. Pydantic schemas for every response. Untrusted-document prompt framing.
Retry-once-then-degrade error handling. Three impact sub-scores plus the editor pass,
within the 25-call budget.

*Artifact: a schema-validated LLM boundary.*

### P6 — Briefing, Telegram and Pages

Select the top five stories. Render the same structured data to a Telegram message and to
static HTML. Deploy the site from Actions.

*Artifact: a live public URL — the single highest-value item on this list.*

### P7 — Claim verification

Extract claims from the highest-ranked events, match each claim against the other sources
inside the same event cluster, and label it `VERIFIED`, `PARTIALLY_VERIFIED`,
`UNVERIFIED` or `CONTRADICTED`. Render the status as a badge on the public site.

*Artifact: a visible, screenshot-able capability that very few comparable projects have.*

### P8 — "What changed" timeline

A page per event showing its development day by day: announced, then available via API,
then adopted. This is what makes the system an intelligence timeline rather than a daily
newsletter.

*Artifact: the story to tell in an interview.*

### P9 — Evaluation

`evals/dataset.json` with 50 hand-labelled historical events, plus an injection corpus of
~40 attacks. Measure duplicate rate, category accuracy, citation accuracy, hallucination
rate and injection escape rate. Publish the numbers in the README and regenerate them in
CI.

*Artifact: numbers. Almost no comparable portfolio project has them.*

### P10 — Observability and documentation

Per-run statistics — feeds fetched, feeds failed, articles ingested, events produced,
deduplication rate, LLM calls, wall-clock runtime — rendered on the public site.
Architecture diagram and decision log in `docs/`.

*Artifact: evidence that it runs unattended.*

P0 through P6 produce a working product with a public URL. P7 through P10 are what make
it worth reading. Do not stop at P6.

---

## 5. Failure policy

| Failure | Response |
| --- | --- |
| One feed fails | Log, record in run stats, continue with the rest |
| LLM call fails validation | Retry once, then mark the event `llm_failed` and continue |
| LLM provider unavailable | Fall back to the deterministic ranking; publish without impact scores |
| Telegram delivery fails | The briefing is already persisted; retry on the next run |
| Pipeline crashes | `state.json` is only advanced on success, so the next run re-covers the window |

Every stage must be restartable.

---

## 6. Success criteria

**V1 is successful when:**

- The daily briefing succeeds on >=95% of days over 30 consecutive days.
- No duplicate story appears within a single briefing.
- Every material claim in a briefing carries a source.
- The recurring cost is zero.
- Reading it each morning is enough to know what happened in AI.

**The resume version is successful when:**

- There is a live public URL with 30+ days of history behind it.
- The README leads with measured numbers, not adjectives.
- CI is green and the badge is real.
- The injection corpus reports zero escapes.

---

## 7. Open questions

- Which free LLM tier to standardise on. Decide during P5 after checking current quotas.
- Whether an event's `category` should be model-assigned or keyword-assigned. Start with
  keywords; revisit if accuracy is poor in P9.
- Whether a gitignored local cache of full article text is worth keeping for
  re-running later phases without re-fetching. Not needed yet.
