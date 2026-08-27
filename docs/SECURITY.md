# Security

What this project defends against, how, and what it does not defend against. The last part
matters most: a security document that lists only wins is marketing.

Nothing here is theoretical. Every control below has tests in `tests/security/` — 55 of
them across four files — and each one exists because the failure it prevents was reachable.

---

## Threat model

AI-Pulse has no user accounts, no database server, no inbound HTTP surface, and no
deployment that accepts a connection. It is a scheduled job that reads the public internet
and writes files. That removes most of the usual attack surface and leaves three real ones.

| Surface | Who controls it | What they could try |
| --- | --- | --- |
| Feed content and redirect targets | Anyone who can publish to a syndicated feed | Reach an internal address through a redirect, exhaust memory with a huge body, hang the run |
| Article text reaching the model | The same people | Prompt injection: steer the analysis, escape the document, corrupt the output |
| Telegram messages | Anyone who finds the bot | Spend the model allowance, enumerate the pipeline, use the bot as an amplifier |

Two more are worth naming because they are the ones that actually leak credentials in
practice: **secrets written to logs**, and **secrets committed to git**.

Out of scope, deliberately: a compromised GitHub account, a compromised laptop, and a
malicious dependency that `pip-audit` does not know about. Those are real, and nothing in
this repository would stop them.

---

## Untrusted input: the network

### The SSRF guard — `app/ingestion/urlguard.py`

Feed URLs come from `config/sources.yaml`, which the repository controls. Redirects do not.
So **every hop is validated, not only the first**.

The check runs on the **resolved address**, never on the hostname. A hostname allowlist or
a regular expression on the host is not a defence: an attacker-controlled domain can
resolve to `127.0.0.1` or to the cloud metadata address `169.254.169.254` exactly as easily
as to a public address. Rejected: private ranges, loopback, link-local, multicast,
reserved, and unspecified addresses, in both IPv4 and IPv6, with IPv4-mapped IPv6 unwrapped
first so that `::ffff:127.0.0.1` cannot slip past.

A hostname whose records **mix public and private addresses is rejected outright** rather
than filtered down to the public ones. A name that resolves to both is not a name this
project needs to fetch.

Only `http` and `https`, only ports 80 and 443. No `file://`, `ftp://`, `gopher://` or
`data:`. A non-standard port is a port scan wearing a URL.

**Residual risk: DNS rebinding.** Validation and connection are separate operations, so a
record whose TTL expires in between could resolve differently the second time. This is
closed rather than accepted: `ValidatedTarget` carries the addresses that passed, and the
fetcher connects to `connect_address` instead of re-resolving the name. The original
hostname is still supplied for TLS SNI, certificate verification and the `Host` header, so
connecting by address costs nothing in correctness.

That leaves a genuinely open edge: a host that legitimately load-balances across many
addresses is pinned to the one that was validated, for that request. Acceptable for feed
fetching, and worth knowing before this module is reused for something else.

### Fetch limits — `app/ingestion/fetcher.py`

Every request in the project goes through one `SafeFetcher`, which is the only reason
limits can be enforced in one place.

| Limit | Default | Why |
| --- | --- | --- |
| Response size | 5 MB, streamed | The body is read in chunks and abandoned the moment it exceeds the cap, so a multi-gigabyte response cannot exhaust memory |
| Connect timeout | 5 s | No request may hang the daily run |
| Read timeout | 15 s | The same |
| Redirects | 3 | Each hop re-validated; a redirect chain is not a free pass |

The size cap is enforced **while streaming**, not by reading the body and checking its
length afterwards, which would be a memory-exhaustion bug wearing a limit.

---

## Untrusted input: the model

`app/llm/prompts.py` is the trust boundary. Everything above it is data the pipeline
controls; everything passed through `wrap_documents` is text harvested from the open
internet and is treated as hostile.

The defence has three parts, and none of them is "ask the model nicely".

**1. The model has no capability to abuse.** It receives structured text and returns
structured JSON. No shell, no filesystem, no browser, no database write, no network. A
successful injection can produce a wrong summary; it cannot take an action, because there
is no action to take. This is the part that actually matters — the prompt instructions are
a second line, not the first. A test asserts that `LLMProvider` exposes no method named
`run`, `execute`, `shell`, `read_file`, `write_file`, `fetch`, `browse` or `tools`, so the
property cannot be lost by accident.

**2. Untrusted text is delimited and labelled.** Article content is wrapped in `<document>`
tags, with any closing tag inside the content neutralised, so a document cannot end its own
container and continue as instructions. Each document is capped at 4,000 characters, which
bounds prompt size, cost, and how much text an attacker controls in one call.

**3. The response is schema-validated.** An injection that succeeds in steering the model
produces output that does not fit the schema, and a response that fails validation is
discarded rather than parsed leniently. One retry, then the event carries only its
deterministic score.

Configuration cannot reach a prompt at all: prompts are built from documents, and a test
asserts that a configured API key appears in neither the wrapped documents nor the system
prompt.

### The limitation, stated plainly

None of this makes the model immune to being *persuaded*. An article can still talk the
model into a misleading summary, and this project cannot detect that. What an article
cannot do is escalate persuasion into an action, a leaked secret, or a corrupted write.

### How it is measured — `app/evals/injection.py`

Two rules keep the number honest.

**An attack is pushed through the real path.** The payload goes into an article body, and
that article goes through the same sanitisation, the same document wrapping, the same
prompt and the same schema validation the pipeline uses. An evaluation that tests a
simplified copy of the defence measures the copy.

**An escape means the output changed, not that the model noticed.** A model that reads
"ignore your instructions", declines, and returns a correctly-shaped analysis has not been
compromised — it has done its job. An escape is one of: a response that fails validation, a
score pushed to the ceiling, the system prompt appearing in the output, or an attribution
to a source that does not exist.

Two layers are reported separately, because they fail independently:

- **`structural`** runs without a model. Does sanitisation neutralise the delimiter, and
  does the payload stay inside its document? **0 escapes of 40.** A failure here is a bug in
  this repository, and CI fails the build on one.
- **`model`** runs against the configured provider. **0 escapes of 40, at full coverage**,
  run on 27 August 2026 against the production chain. This number was unavailable until the
  chain existed: one free tier's daily allowance ran out after three or four attacks, and
  the report said what share had reached the model rather than quoting an escape count,
  because a number earned at partial coverage is not the number. Two tiers finish the
  corpus. A failure here is a property of the model rather than a bug in this repository,
  and the schema is what contains it.

---

## Untrusted input: the bot

`app/delivery/bot.py` answers Telegram messages. Anyone who finds the bot can message it.

**Owner-only by default.** A message from a chat other than `AI_PULSE_TELEGRAM_CHAT_ID` is
answered with *nothing at all* — not an error. An error reply confirms the bot exists, is
alive, and is worth probing.

**Public read-only mode** (`AI_PULSE_PUBLIC_READ_ONLY=true`) exists so the bot can be
demonstrated. It changes exactly what it says:

- A guest gets the briefing that is already committed to the repository — stored data, and
  nothing that costs.
- `/refresh` and `/status` are refused with a message rather than ignored, because a guest
  who typed `/refresh` should learn why nothing happened. `/refresh` spends the model
  allowance and `/status` describes the pipeline's internals; neither is a guest's to have.
- Repeat replies to one chat are rate limited. Over the limit the bot is silent rather than
  saying "slow down", which would itself be a reply worth spamming for.
- The workflow that runs in this mode (`bot.yml`) supplies **no model key at all**, so the
  job cannot spend the allowance even if a command slipped through the checks above.

Every update is acknowledged, including ones the bot ignores, so an unparseable message is
not re-delivered forever.

---

## Output

Article titles and summaries are attacker-influenced text that ends up in two rendered
surfaces, and both escape on the way in rather than trusting the source.

- **The static site** — `app/briefing/render_html.py` escapes every interpolated value with
  `html.escape(..., quote=True)`.
- **Telegram** — messages are sent with `parse_mode: HTML`, where a single unescaped `<`
  breaks the message and a crafted title could inject markup. `render_telegram.py` escapes
  everything on the way in, without exception, and URLs with `quote=True`.

---

## Secrets

Credentials live in exactly two places: `.env` locally, gitignored, and GitHub Actions
secrets in CI. Neither is ever written into the repository.

**`.env` is gitignored and must never be committed.** Verify with `git check-ignore -v .env`
before doing anything clever with it.

**An empty environment variable is treated as unset.** `.env.example` ships every key blank,
and a blank value must not be mistaken for a configured one — a validator in
`app/core/config.py` normalises empty strings to `None`.

**Never log a Telegram API URL.** The bot token sits in the URL *path*, and `httpx` logs
request URLs at INFO. Left alone that writes a live credential to the log file on every
delivery, and into any log a user shares while asking for help. `httpx` and `httpcore` are
pinned to WARNING in `app/jobs/daily_briefing.py`, and a test asserts that no request URL is
logged at INFO. This was a real defect, found and fixed, not a hypothetical.

**Rotate by replacing, then revoking.** Set the new key in `.env` and in the repository
secret, confirm a run authenticates with it, and only then revoke the old one at the
provider. A working key that nothing uses is a key nobody notices leaking.

**A key that lists models is not proof of anything.** A Cerebras key returned a full model
list and `402 Payment Required` on every chat completion. Test the call you actually make.

---

## Supply chain

- **Dependencies are pinned.** `requirements.lock` and `requirements.runtime.lock` hold
  fully resolved versions, and CI installs from them rather than from a range.
- **`pip-audit` runs in CI** on every push and pull request, against those lockfiles.
- **Dependabot** opens pull requests weekly for Python packages and monthly for GitHub
  Actions versions — which are code that runs with repository write access and a secrets
  context. Routine minor and patch bumps are grouped into one pull request so that a major
  bump, the thing most likely to break the pipeline, arrives alone and gets read.
  Dependabot security updates and vulnerability alerts are enabled, so a published advisory
  produces a pull request rather than waiting for the next CI run to notice.
- **Secret scanning and push protection** are enabled on the repository. Push protection is
  the one that matters: a commit containing a recognisable credential is rejected at push
  time rather than found afterwards, when the only fix is revocation.
- **Workflow permissions** are declared explicitly and are read-only everywhere except the
  daily run, which needs `contents: write` to commit `data/` and `pages: write` with
  `id-token: write` to deploy. The bot workflow, which is the one a stranger can reach,
  holds `contents: read` and no model key.

---

## What CI enforces

Security is a build step, not a review habit. Each push runs:

```
ruff check .          # includes flake8-bandit (S) and flake8-blind-except (BLE)
ruff format --check .
mypy                  # strict
pytest                # includes tests/security/
pip-audit             # against the pinned lockfiles
python scripts/eval.py  # fails the build on a structural injection escape
```

The `S` and `BLE` rule sets are the relevant ones here: `S` catches the common insecure
patterns, and `BLE` prevents a bare `except Exception` from swallowing a security control's
failure into a silent pass.

---

## Reporting a vulnerability

This is a personal project with one maintainer and no bounty.

**Private vulnerability reporting is enabled.** For anything that should not be public
first, use *Report a vulnerability* on the repository's
[Security tab](https://github.com/Mani5266/ai-pulse/security). For anything low-risk, an
ordinary issue is fine.

A fix will follow when time allows. There is no SLA, and publishing one would be
dishonest.

Please do not test against the live deployment in any way that spends the model allowance
or floods the bot — both are free tiers, and exhausting them takes the project down for the
day.
