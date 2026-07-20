"""Small, explicit text-matching helpers for the deterministic rules.

Word-boundary aware phrase search plus a context helper that decides whether a
mention (e.g. an advanced degree or a years-of-experience figure) reads as
*required* vs merely *preferred*. No NLP, no external deps.
"""

from __future__ import annotations

import re

# Words that soften a requirement into a preference (not disqualifying).
PREFERRED_CUES = (
    "preferred",
    "a plus",
    "plus",
    "nice to have",
    "nice-to-have",
    "bonus",
    "ideal",
    "ideally",
    "desired",
    "or equivalent",
    "not required",
    "helpful",
)

# Words that indicate a hard requirement.
REQUIRED_CUES = (
    "required",
    "must",
    "minimum",
    "at least",
    "requirement",
    "need",
    "you have",
    "we require",
)


def normalize(text: str | None) -> str:
    """Lowercase and collapse whitespace/punctuation runs for robust matching.

    Apostrophes are removed (not spaced) so "Master's" -> "masters" and matches
    the degree vocabulary; other punctuation becomes whitespace."""
    if not text:
        return ""
    text = text.lower().replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z0-9+.\-/ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _boundary_pattern(phrase: str) -> re.Pattern[str]:
    # Escape, but treat spaces as flexible whitespace and keep +/./- literal.
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def find_first(text: str, phrases: tuple[str, ...] | list[str]) -> str | None:
    """Return the first phrase that occurs in text (word-boundary aware)."""
    for phrase in phrases:
        if _boundary_pattern(phrase).search(text):
            return phrase
    return None


def contains_any(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    return find_first(text, phrases) is not None


def context_is_preferred(text: str, phrase: str, window: int = 60) -> bool:
    """True if the mention of `phrase` reads as preferred rather than required."""
    m = _boundary_pattern(phrase).search(text)
    if not m:
        return False
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    ctx = text[start:end]
    return contains_any(ctx, PREFERRED_CUES) and not contains_any(ctx, REQUIRED_CUES)
