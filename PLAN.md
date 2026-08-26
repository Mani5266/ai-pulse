# AI-Pulse — Build Plan

**Status:** P7 complete
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

**Caveat:** GitHub's scheduled workflows can be delayed under load. The pipeline
therefore windows on `last_briefing_at` in `data/state.json`, never on "the last 24 hours",
so a delayed or missed run is picked up rather than lost. See §2.13 — this was specified
here from the start, was not built until it failed in the reader's hands, and is the single
most expensive omission in the project so far.

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
(Gemini, Groq, Cerebras, OpenRouter free models). **Groq is the chosen hosted provider**,
for its OpenAI-compatible API shape: swapping to another OpenAI-compatible free tier is a
base-URL change rather than a rewrite. Local development uses Ollama with `qwen3:4b`,
which fits the 4 GB of VRAM on the development machine.

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

### 2.8 Title similarity needs an identity guard

Character trigrams alone are unsafe on AI headlines. Measured on one real day of 22
feeds, *every* title pair scoring above 0.85 was a false positive, and each differed only
in a number or a month:

```
0.91  "The latest AI news we announced in July 2026"  /  "... in June 2026"
0.90  "sqlite-utils 4.2.1"                            /  "sqlite-utils 4.2"
0.83  "Introducing Gemini 3.6 Flash, 3.5 Flash-Lite"  /  "Introducing Gemini 3.5 Flash Cyber"
```

Two monthly roundups, two releases, two model announcements — all would have been merged.
Trigram similarity is nearly blind to a three-character difference in a forty-character
string, and in AI news that difference is frequently the entire identity of the item.

So a merge requires both a high similarity score *and* an identical set of identity
tokens: every number and every month name in the title. After the guard, that day
produced zero title merges, which is the correct answer — the pairs were not duplicates.

### 2.9 Clustering is tuned for precision, and under-clusters

Grouping articles into events is where the product lives or dies, and it is the stage
where string overlap runs out of road. Three rules survived contact with one real day of
22 feeds; each replaced something that failed visibly.

**Entities are weighted by specificity.** Treating them alike merged twenty-two separate
OpenAI articles into one event, because they all said "OpenAI". An organisation is worth
0.25, a model family or acronym 0.6, a product 0.7, a model family *with a version* 1.0.

**A publisher does not report its own news twice.** Nine unrelated `openai.com` posts
merged on the word "ChatGPT" alone. Two articles from the same source now need
near-duplicate wording (0.60) to join, rather than a shared entity.

**Only a model version merges on entities alone.** Everything weaker needs the wording to
agree as well. An attempt to relax this — mining distinctive capitalised words like
"Cowork", "Nemotron", "SageMaker" and treating them as decisive — collapsed on contact: a
single shared "Desktop" merged a story about SpaceX shares with Anthropic's Cowork launch,
and multi-source events jumped from 8 to 88, nearly all wrong.

The measured result on that day: 515 articles into 490 events, 9 of them corroborated by
more than one source. Roughly two thirds of those nine are correct on inspection.

**The honest limitation.** Title similarity cannot separate the remaining cases. On the
true and false pairs from that day, the true ones scored 0.20, 0.23, 0.28 and 0.65 and the
false ones 0.15, 0.26 and 0.36 — they interleave, so no threshold divides them. Two
outlets describing one event in genuinely different words stay two events unless they name
the same model version.

Closing that gap needs semantics, not string overlap. That is a good use of the P5 budget:
adjudicating a shortlist of borderline pairs is exactly the judgement worth a model call,
and exactly the guess a keyword rule must not make.

### 2.10 The shortlist is where zero cost is actually won

Roughly 500 events a day must become at most 20 before the first model call, and a plain
top-N by score is the wrong cut twice over.

**Volume would win.** Research produces eighty events a day; every other category produces
a handful. Top-N hands the briefing to arXiv. So the shortlist takes the best of each
category in turn, capped at four.

**Corroboration would lose.** An event covered by three independent sources is the
strongest importance signal available without a model — and on live data the category cap
was dropping exactly those: a Gemma 4 release covered by three sources lost its slot to
four single-source blog posts in the same category. Corroborated events therefore bypass
the cap. The cap exists to stop volume dominating, and corroboration is the opposite of
volume.

Measured on one real day: 490 events considered, 20 shortlisted across six categories,
five of them corroborated, scores from 8.00 down to a 6.83 cut-off.

### 2.11 The model gets data, never authority — and never the whole budget

Three properties, each enforced by code rather than by prompt wording.

**No capability to abuse.** The provider exposes exactly one method, `structured()`. No
shell, no filesystem, no browser, no database write, and no free-text `generate()`. A
successful prompt injection can produce a wrong summary; it cannot take an action, because
there is no action to take.

**No unvalidated output.** Every call declares a Pydantic schema with bounded fields and
`extra="forbid"`. A model that returns 9999 for an impact score, or echoes the system
prompt, fails validation and the response is discarded rather than coerced. That is what
turns a successful injection into a dropped call instead of a corrupted briefing.

**No runaway spend.** The provider refuses calls past its budget, and impact scoring holds
back a reservation for the summaries. The first live run made the case for it: twenty
events each retrying once consumed the entire forty-call allowance and every summary was
skipped. Scoring degrades gracefully — the deterministic score stands — but a briefing
with no prose does not.

**And the free tier limits tokens, not requests.** Groq allows 8,000 tokens per minute on
the chosen model, against 1,000 requests per day — so the binding constraint is prompt
size, not call count. The first hosted run sent 4,000 characters per scoring call and 14 of
25 calls failed, because a token ceiling was being retried against immediately rather than
waited out. Two fixes: the provider now reads the reset headers and sleeps (refunding the
attempt, since waiting is not a failed try), and scoring sends 600 characters from each of
three articles where summarising still sends 1,500 from four. Scoring needs to know what
happened; only summarising needs to read. Re-measured: 25 of 25 calls succeeded, three
short waits, 2m24s end to end.

The residual risk is stated rather than hidden: none of this stops the model being
*persuaded* into a misleading summary by a well-written article. P9 measures how often
that succeeds.

### 2.12 Render once, from data, and never pad a gap

The briefing is a structured record first and text second. Both renderers read the same
`Briefing` object, which is what stops the Telegram message and the web page from drifting
apart as either is edited, and what lets the whole archive be regenerated from committed
JSON — no model calls, no network — when a rendering detail changes.

Two rules in that stage are worth defending:

**A story without a supported summary is dropped.** Falling back to the article's own
headline and blurb would be easy, would read perfectly well, and would convert the product
into a feed reader wearing a briefing's clothes. Four verified stories beat five where one
is unverified, and an empty day says so in as many words.

**Everything is escaped, in both renderers.** Every string in a briefing began as text
someone published on the internet and passed through a model. In Telegram's HTML parse
mode an unescaped `<` breaks the message and a crafted title could inject markup; on the
page the same applies. The escaping is not defensive habit, it is the last segment of the
same untrusted-input path that starts at the RSS fetcher.

### 2.13 Corroboration is rarer than the design assumed

Verified against live feeds, and worth recording because it changes what the feature is
for.

Three real articles about Gemma 4, from Google DeepMind, Hugging Face and Ollama, produced
four claims — and every one came back `UNVERIFIED`. Correctly: the three articles say
*different* things about the same model. One announces it, one integrates it, one
benchmarks it. None asserts the same fact as another, so there is nothing to corroborate.
The model did not inflate attribution to manufacture agreement, which is the failure this
design exists to prevent.

The same day's full run produced **zero** multi-source events out of 121.

Two consequences. First, the honest label for most AI news is "single source", because
most AI news is an organisation announcing its own work — and saying so plainly is worth
more than a rare `VERIFIED` badge. Second, corroboration as a *ranking* signal is weaker
than §2.10 assumed, since it fires on so few events; that is a finding for the P9
evaluation to quantify rather than a bug to fix here.

### 2.14 A feed's window is not "what is new"

The defect that reached the reader's phone, and the most instructive one so far.

RSS hands over a publisher's *current window*, and its size is entirely the publisher's
choice. TechCrunch's twenty items are one day; a working engineer's blog with twenty items
is a year. The pipeline ingested all of it and treated every item as news: 517 articles
whose publication dates spanned thirteen months, and a "daily briefing" led by a model
release from four months earlier. Only 22% of what was ingested was less than a day old.

Nothing in the pipeline was wrong in isolation. Dedup, clustering and ranking all worked
exactly as designed — on the wrong input.

The fix is `data/state.json` plus a recency filter:

- The window runs from the **last successful briefing** to now, so a delayed, skipped or
  doubled run picks up what it missed instead of losing it.
- A first run looks back a short default (2 days), because there is nothing to anchor to.
- A long gap is **clamped** (7 days), because "everything since" after a fortnight is a
  briefing nobody reads.
- State advances only after a briefing is actually produced, so a crash re-covers the
  window rather than skipping it.
- The briefing header prints the window it covered. A briefing headed "Wednesday" that
  reports four days of news is lying to its reader.

Filtering happens after fetching, not during: the older half of a feed is still worth
having, because it is what lets deduplication and clustering recognise a story they have
already seen.

Measured on the same feeds: 515 articles in, 123 in-window, 392 stale — and the oldest
story in the briefing is now two days old rather than four months.

**The lesson worth keeping.** §2.1 specified this windowing from the first day of the
project and it was not built for six phases, because every stage tested green on data that
was quietly wrong. Unit tests cannot catch an input assumption; only reading the actual
output can, and in this case a reader did it first.

### 2.14 arXiv needs its own quota

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

### P2 — Canonicalization and deduplication — done

Canonical URLs (https-normalised, `www` stripped, tracking parameters removed, query
sorted, AMP and index suffixes stripped), a 64-bit article id derived from the canonical
URL, a content hash over normalised title and summary, and title trigram similarity
guarded by identity tokens. Three passes, cheapest first. No embeddings, no model.

Deduplication also reads the last seven days of stored history, so a feed that keeps
listing last week's post cannot present it as news again.

*Artifact: 62 tests, several built from false positives found on live data.*

### P3 — Event clustering — done

Entity extraction (lexicon plus version patterns), keyword categorisation into 14
categories, and single-pass clustering that blends weighted entity overlap with title
similarity. Events carry `id`, `canonical_title`, `category`, `entities`, `article_ids`,
`source_ids`, `first_seen` and `last_updated`.

Events from the last 14 days take part in matching, so an article published today extends
Monday's event and moves its `last_updated` — the foundation the P8 timeline reads back.
Event snapshots are append-only per day; the reader keeps the latest, so Monday's file
still says what Monday knew.

*Artifact: the core differentiator exists in code, with its precision limits measured and
documented rather than assumed.*

### P4 — Deterministic scoring — done

`credibility` from the source registry plus capped corroboration, `novelty` from event and
entity history, `personal_relevance` from `config/profile.yaml` and the category weights.
Three sub-scores at 0.15 each: 45% of the importance formula, computed with no model
involved. The remaining 55% is the three impact scores from P5.

Ties break deterministically — corroboration, then recency, then id — so two runs over the
same data produce the same order, which is what makes P9 evaluation possible.

*Artifact: a reproducible ranking, and a shortlist that cuts 490 events to 20.*

### P5 — LLM layer — done

`LLMProvider` with a single method, `structured()`. There is deliberately no `generate()`
returning free text: every call site declares a Pydantic schema, so no unvalidated model
output can reach the application. Two implementations — Groq for CI and production, Ollama
with `qwen3:4b` for local development.

Untrusted article text is sanitised and wrapped in `<document>` tags whose delimiters
cannot be escaped. Responses that fail validation are discarded, never coerced. Every
failure degrades: a scoring failure keeps the deterministic score, a summary failure drops
the story rather than publishing it unsupported.

The provider enforces the call budget itself, so a loop bug cannot exhaust a free tier
overnight, and impact scoring reserves budget for the summaries that follow.

*Artifact: a schema-validated model boundary, and 30 injection tests.*

### P6 — Briefing, Telegram and Pages — done

The briefing is built once as structured data and rendered twice, to Telegram and to
static HTML, so the two outputs cannot drift apart and history can be re-rendered without
re-running the model.

A story with no model-written summary is **dropped, not padded** with the article's own
headline. That fallback would read fine and would quietly turn the product into a feed
reader while still looking like a briefing.

Delivery happens last and on purpose: the briefing is persisted and the site rebuilt
before Telegram is called, so a failure at the one stage that depends on somebody else's
server costs nothing and retries next run.

*Artifact: a briefing on the phone, and a self-contained page per day with every claim
linked to its source. Deploying to Pages needs the repository pushed.*

### P7 — Claim verification — done

The model extracts claims and says which documents assert each one. **Code assigns the
label**, by counting independent sources — the same split as the ranking formula, and for
the same reason: a model asked "is this verified?" answers confidently either way and
cannot be checked, while an attribution can be compared against the documents and clicked
through by a reader.

An attribution to a source the event does not have is discarded rather than counted, since
the failure mode of a verification feature is verifying things by inventing witnesses. Two
articles from one publisher count once: they are not two observations.

Single-source events skip the call entirely. A lone source cannot corroborate itself, so
the source count already gives the answer, and on a typical day that is most of the
shortlist.

*Artifact: badges on the public site, and 21 tests pinning the label logic.*

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
