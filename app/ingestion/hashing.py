"""Identity and content hashes.

Two different questions, two different hashes:

* ``article_id`` answers *is this the same URL?* It is derived from the canonical URL, so
  the same article under five tracking-parameter variants gets one id.
* ``content_hash`` answers *is this the same text?* It is derived from normalised title
  and summary, so a syndicated copy under a different URL still collides.

Both are truncated SHA-256. Truncation is safe here because a collision costs one merged
article in a personal news briefing, not a security boundary — and 64 bits of id is far
beyond the roughly 500 articles a day this pipeline handles.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

ARTICLE_ID_CHARS = 16
"""64 bits. At 500 articles/day, a collision is not a practical concern."""

CONTENT_HASH_CHARS = 32

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    """Reduce text to a comparison form.

    Unicode is normalised (NFKC), case is folded, punctuation is dropped and whitespace
    is collapsed. The point is that a smart quote, a stray em dash or a doubled space
    must not make two identical headlines look different::

        "OpenAI's  new model — GPT-X"  ->  "openais new model gptx"
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = _PUNCTUATION.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def _digest(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def article_id(canonical_url: str) -> str:
    """Stable identity for one article, derived from its canonical URL."""
    return _digest(canonical_url, ARTICLE_ID_CHARS)


def content_hash(title: str, summary: str | None = None) -> str:
    """Hash of an article's normalised text.

    Title and summary are joined with a separator that cannot occur in normalised text,
    so ``("ab", "c")`` and ``("a", "bc")`` cannot collide.
    """
    return _digest(f"{normalize_text(title)}\x00{normalize_text(summary)}", CONTENT_HASH_CHARS)
