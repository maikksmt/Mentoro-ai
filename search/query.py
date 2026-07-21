"""
Query normalization for the global search.

Pure Python: no database, no request, no active language, no Django models.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

MIN_SEARCH_QUERY_LENGTH = 2
MAX_SEARCH_QUERY_LENGTH = 100


class SearchQueryIssue(StrEnum):
    """Why a raw query cannot be searched. `None` instead means "usable"."""

    EMPTY = "empty"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"


def normalize_text(text: str) -> str:
    """
    Applies the shared text normalization used for queries, title comparison
    and snippet bodies: NFKC, control characters removed, whitespace collapsed.

    Whitespace control characters (tab, newline, ...) are category Cc too, so
    they are kept as separators here and folded into single spaces by the
    collapse step - only non-whitespace control characters (NUL, ESC, ...) are
    dropped outright.
    """
    normalized = unicodedata.normalize("NFKC", text)
    without_controls = "".join(
        char
        for char in normalized
        if char.isspace() or unicodedata.category(char) != "Cc"
    )
    return " ".join(without_controls.split())


@dataclass(frozen=True, slots=True)
class NormalizedSearchQuery:
    """
    A user query after normalization, plus the reason it is unusable (if any).

    `value` stays display-ready: original casing, punctuation, hyphens,
    umlauts, accents and emoji are preserved. A too-long query is flagged
    rather than silently truncated, so the caller can say so instead of
    searching for something the user did not type.
    """

    value: str
    issue: SearchQueryIssue | None

    @property
    def is_searchable(self) -> bool:
        return self.issue is None


def normalize_search_query(raw_query: str | None) -> NormalizedSearchQuery:
    """
    Normalizes `raw_query` and classifies it as searchable or not.

    Length is measured on the normalized value, so trailing whitespace or
    control characters never push a query over the limit.
    """
    value = normalize_text(raw_query or "")

    if not value:
        return NormalizedSearchQuery(value=value, issue=SearchQueryIssue.EMPTY)
    if len(value) < MIN_SEARCH_QUERY_LENGTH:
        return NormalizedSearchQuery(value=value, issue=SearchQueryIssue.TOO_SHORT)
    if len(value) > MAX_SEARCH_QUERY_LENGTH:
        return NormalizedSearchQuery(value=value, issue=SearchQueryIssue.TOO_LONG)
    return NormalizedSearchQuery(value=value, issue=None)
