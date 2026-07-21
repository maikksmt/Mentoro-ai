"""
The contract every search adapter fulfils.

Structural typing only: adapters are plain classes that happen to match this
shape. Nothing checks it at runtime, so it stays documentation the type
checker can verify rather than machinery to maintain.
"""
from __future__ import annotations

from typing import Protocol

from search.query import NormalizedSearchQuery
from search.result_types import SearchResult, SearchResultKind


class SearchAdapter(Protocol):
    """
    Finds public content of one kind.

    Implementations fail closed - an unsearchable query, an unsupported
    language or a backend without full-text search raises rather than
    returning an empty tuple, because an empty result and "the search could
    not run" must never look the same to the caller.
    """

    kind: SearchResultKind

    def search(
        self,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> tuple[SearchResult, ...]:
        ...
