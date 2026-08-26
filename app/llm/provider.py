"""LLM provider abstraction.

Two implementations, chosen by environment, and the split is the reason the abstraction
exists rather than speculative generality:

===================== ==================== =================================================
Environment           Provider             Why
===================== ==================== =================================================
CI and production     Groq (hosted)        Ollama cannot run on a GitHub-hosted runner
Local development     Ollama               No quota, no network, no key, fast iteration
===================== ==================== =================================================

Both are reached through :meth:`LLMProvider.structured`, which is the only method the
pipeline uses. There is deliberately no ``generate()`` returning free text: every call
site declares a schema, so no unvalidated model output can reach the application.

**The budget is enforced here, not by convention.** A provider is constructed with a call
budget and refuses further calls once it is spent, so a bug in a loop cannot exhaust a free
tier at three in the morning.

**Failure is a value, not an exception.** A provider returns ``None`` when a call fails
after its retry, and the pipeline degrades to the deterministic ranking rather than
aborting the run. A briefing without impact scores is worse than one with them; no briefing
at all is worse than both.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import AIPulseError
from app.llm.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LLMError(AIPulseError):
    """A model call failed. Always recoverable: the pipeline degrades instead."""


class BudgetExhaustedError(LLMError):
    """The per-run call budget is spent."""


class RateLimitedError(LLMError):
    """The provider is rate limited, and told us how long to wait.

    Distinct from a generic failure because the response is different: waiting works,
    while retrying immediately burns budget to be refused again. Groq's free tier limits
    tokens per minute rather than requests, so a run of twenty scoring calls hits it
    routinely — measured, on the first hosted run: 14 of 25 calls failed and the budget
    was exhausted, purely from immediate retries against a token ceiling.
    """

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class CallStats:
    """What this run spent. Reported in the run log and the P10 dashboard."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    invalid_json: int = 0
    schema_violations: int = 0
    rate_limited: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "retried": self.retried,
            "invalid_json": self.invalid_json,
            "schema_violations": self.schema_violations,
            "rate_limited": self.rate_limited,
            "seconds": round(self.seconds, 2),
        }


def extract_json(text: str) -> str:
    """Pull the JSON object out of a model response.

    Models wrap JSON in markdown fences or add a sentence of preamble despite being told
    not to. Recovering from that is not leniency about *content* — the result still has to
    validate against the schema — it is leniency about packaging, which costs nothing and
    avoids discarding an otherwise correct response.
    """
    stripped = _FENCE.sub("", text.strip())
    match = _JSON_BLOCK.search(stripped)
    return match.group(0) if match else stripped


class LLMProvider(ABC):
    """A source of schema-validated structured output."""

    def __init__(self, *, budget: int, max_backoff: float = 30.0, max_waits: int = 40) -> None:
        self._budget = budget
        self._max_backoff = max_backoff
        self._max_waits = max_waits
        self.stats = CallStats()

    @property
    def remaining(self) -> int:
        return max(0, self._budget - self.stats.attempted)

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifier for logs and the run record."""

    @abstractmethod
    def _complete(self, prompt: str) -> str:
        """Send one prompt and return the raw response text. May raise."""

    def structured(self, prompt: str, schema: type[T], *, retries: int = 1) -> T | None:
        """Call the model and validate the response against ``schema``.

        Returns ``None`` rather than raising when the call fails or the response cannot be
        validated, because one unusable response must not end the run. The only exception
        is an exhausted budget, which is a programming error worth surfacing.

        The retry is a single re-ask. Models that return malformed JSON once often succeed
        on a second attempt; a model that fails twice is not going to succeed on a third,
        and each attempt spends budget.
        """
        for attempt in range(retries + 1):
            if self.remaining <= 0:
                raise BudgetExhaustedError(f"{self.name}: call budget of {self._budget} exhausted")

            self.stats.attempted += 1
            if attempt:
                self.stats.retried += 1

            try:
                raw = self._complete(prompt)
            except RateLimitedError as exc:
                # Waiting is the correct response; an immediate retry is refused again and
                # costs budget. The attempt is refunded so a slow provider does not eat
                # the allowance.
                wait = min(exc.retry_after, self._max_backoff)
                self.stats.attempted -= 1
                self.stats.rate_limited += 1
                if self.stats.rate_limited > self._max_waits:
                    logger.warning("%s: rate limited too often, giving up", self.name)
                    break
                logger.info("%s: rate limited, waiting %.1fs", self.name, wait)
                time.sleep(wait)
                continue
            except Exception as exc:  # noqa: BLE001 - every provider failure is recoverable
                logger.warning("%s: call failed: %s: %s", self.name, type(exc).__name__, exc)
                continue

            try:
                payload = json.loads(extract_json(raw))
            except json.JSONDecodeError as exc:
                self.stats.invalid_json += 1
                logger.warning("%s: response was not JSON: %s", self.name, exc)
                continue

            try:
                validated = schema.model_validate(payload)
            except ValidationError as exc:
                self.stats.schema_violations += 1
                # A response that does not fit the schema is discarded, never coerced.
                # This is what turns a successful prompt injection into a dropped call.
                logger.warning("%s: response failed %s: %s", self.name, schema.__name__, exc)
                continue

            self.stats.succeeded += 1
            return validated

        self.stats.failed += 1
        return None


class OllamaProvider(LLMProvider):
    """Local inference. The development path.

    Talks to ``localhost`` only. The application never reaches the internet for inference
    in this mode, which is what makes prompt iteration free and offline.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        super().__init__(budget=settings.llm_call_budget)
        self._model = settings.llm_model
        self._host = settings.ollama_host.rstrip("/")
        self._think = settings.ollama_think
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(settings.llm_timeout))

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _complete(self, prompt: str) -> str:
        response = self._client.post(
            f"{self._host}/api/chat",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": "json",
                "think": self._think,
                "options": {"temperature": 0.2},
            },
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("ollama returned an empty response")
        return content


def _retry_after(response: httpx.Response, *, default: float = 10.0) -> float:
    """How long the provider says to wait, in seconds.

    Groq reports both a request and a token reset; the token one is what bites on the free
    tier, so the longer of the two is used.
    """
    candidates: list[float] = []
    for header in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        value = response.headers.get(header)
        if value:
            parsed = _parse_duration(value)
            if parsed is not None:
                candidates.append(parsed)
    return max(candidates) if candidates else default


def _parse_duration(value: str) -> float | None:
    """Parse "43.455s", "2m30s" or a bare number of seconds."""
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass

    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", value)
    if not match or not any(match.groups()):
        return None
    minutes = float(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return minutes * 60 + seconds


class GroqProvider(LLMProvider):
    """Hosted inference on Groq's free tier. The CI and production path.

    Groq speaks the OpenAI chat-completions shape, which keeps this implementation small
    and makes swapping to another OpenAI-compatible free tier a base-URL change rather
    than a rewrite.
    """

    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        super().__init__(budget=settings.llm_call_budget)
        if not settings.llm_api_key:
            raise LLMError("AI_PULSE_LLM_API_KEY is required for the hosted provider")
        self._model = settings.llm_model
        self._base_url = (settings.llm_base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.llm_timeout),
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return f"groq:{self._model}"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _complete(self, prompt: str) -> str:
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        )
        if response.status_code == 429:
            raise RateLimitedError("groq: rate limited", _retry_after(response))
        response.raise_for_status()
        choices = response.json().get("choices", [])
        if not choices:
            raise LLMError("groq returned no choices")
        content = choices[0].get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("groq returned an empty response")
        return content


@dataclass
class ScriptedProvider(LLMProvider):
    """A provider that returns canned responses. For tests only.

    Every behaviour that matters — budget enforcement, retry, malformed JSON, schema
    violation, injected output — is exercised through this rather than a live model, so
    the test suite is deterministic, offline and free.
    """

    responses: list[str] = field(default_factory=list)

    def __init__(self, responses: list[str], *, budget: int = 25) -> None:
        super().__init__(budget=budget)
        self.responses = list(responses)
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "scripted"

    def _complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise LLMError("scripted provider ran out of responses")
        return self.responses.pop(0)


def build_provider(settings: Settings) -> LLMProvider:
    """Construct the provider the configuration asks for."""
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings)
    return GroqProvider(settings)
