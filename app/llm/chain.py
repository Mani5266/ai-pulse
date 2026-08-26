"""A chain of free-tier providers, tried in order.

Every free allowance runs out. Groq's is 200,000 tokens a day, which is about five runs;
Cerebras and OpenRouter have their own. One key is therefore a single point of failure with
a known failure time, and the pipeline's answer until now was to degrade to the
deterministic ranking and publish a briefing with no prose.

A chain removes that. When a provider says its daily allowance is spent, the next one takes
over mid-run, and the briefing is finished rather than truncated. Three free tiers is
roughly fifteen runs a day where one gave five.

**Only a quota failure advances the chain.** A malformed response, a schema violation, a
timeout — none of those mean the provider is finished, and moving on would burn a second
allowance on a problem the second provider shares. The chain advances on
:class:`DailyQuotaExceededError` and on nothing else.

**Order is preference, not fallback quality.** The first provider should be the one whose
output you want; the rest are there so the run completes. Put the best model first and
accept that some days end on the second-best.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel

from app.llm.provider import CallStats, LLMProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ChainProvider(LLMProvider):
    """Delegates to the first provider that still has allowance left."""

    def __init__(self, providers: Sequence[LLMProvider]) -> None:
        if not providers:
            raise ValueError("a chain needs at least one provider")
        # The chain's own budget is the sum of its links', since spending is delegated.
        super().__init__(budget=sum(provider.remaining for provider in providers))
        self._providers = list(providers)
        self._index = 0

    @property
    def name(self) -> str:
        return "chain(" + ", ".join(provider.name for provider in self._providers) + ")"

    @property
    def current(self) -> LLMProvider:
        return self._providers[min(self._index, len(self._providers) - 1)]

    @property
    def exhausted(self) -> bool:
        """True when every link has spent its allowance."""
        return self._index >= len(self._providers)

    @property
    def stats(self) -> CallStats:
        """Call statistics summed across the chain.

        Reported as one figure because the pipeline asks "what did this run cost", not
        "what did each provider cost". The per-provider detail stays on the links.
        """
        total = CallStats()
        for provider in self._providers:
            total.attempted += provider.stats.attempted
            total.succeeded += provider.stats.succeeded
            total.failed += provider.stats.failed
            total.retried += provider.stats.retried
            total.invalid_json += provider.stats.invalid_json
            total.schema_violations += provider.stats.schema_violations
            total.rate_limited += provider.stats.rate_limited
            total.quota_exhausted = self.exhausted
        return total

    @stats.setter
    def stats(self, value: CallStats) -> None:
        # The base class assigns in __init__; the chain computes instead, so this is a
        # deliberate no-op rather than an error.
        return

    @property
    def remaining(self) -> int:
        return sum(provider.remaining for provider in self._providers[self._index :])

    def close(self) -> None:
        for provider in self._providers:
            closer = getattr(provider, "close", None)
            if callable(closer):
                closer()

    def _complete(self, prompt: str) -> str:
        # Never called: structured() delegates to a link rather than completing itself.
        raise NotImplementedError("ChainProvider delegates; it does not complete")

    @staticmethod
    def _is_spent(provider: LLMProvider) -> bool:
        """Whether a link has just reported its daily allowance gone.

        Read through a call rather than inline, because the same expression is tested
        before the delegated call and a type checker will otherwise assume a property
        cannot change across it. It can: that call is what sets the flag.
        """
        return provider.stats.quota_exhausted

    def _advance(self, reason: str) -> None:
        spent = self.current.name
        self._index += 1
        if self.exhausted:
            logger.warning("every provider is spent (%s on %s)", reason, spent)
        else:
            logger.warning("%s on %s; switching to %s", reason, spent, self.current.name)

    def structured(self, prompt: str, schema: type[T], *, retries: int = 1) -> T | None:
        """Ask the first provider with allowance left, advancing only on quota."""
        for _ in range(len(self._providers)):
            if self.exhausted:
                break

            provider = self.current

            if provider.remaining <= 0 or self._is_spent(provider):
                self._advance("no allowance left")
                continue

            result = provider.structured(prompt, schema, retries=retries)

            if result is not None:
                return result

            if self._is_spent(provider):
                self._advance("daily allowance spent")
                continue

            # A genuine failure — bad JSON, a schema violation, a timeout. The next
            # provider would most likely fail the same way, and trying it would spend a
            # second allowance to find out.
            return None

        return None
