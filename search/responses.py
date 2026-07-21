"""
What one global search returns.

Pure data, immutable, template-friendly: no querysets, no model instances, no
request, no pagination metadata and no timing. A view decides how to present
this; it never has to reach past it.
"""
from __future__ import annotations

from dataclasses import dataclass

from search.query import NormalizedSearchQuery
from search.result_types import SearchResult, SearchResultKind

#: Stable order for the per-kind counts. Presentation order only - it has no
#: influence on how results themselves are sorted.
COUNT_ORDER: tuple[SearchResultKind, ...] = tuple(SearchResultKind)


@dataclass(frozen=True, slots=True)
class SearchKindCount:
    """How many results one content type contributed."""

    kind: SearchResultKind
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError(f"count must not be negative, got {self.count!r}")


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """
    One completed global search.

    ``results`` is the full, globally sorted match set - never a page and
    never a capped candidate list. ``counts`` lists every content type,
    including those that contributed nothing, so a caller can render an
    overview without special-casing absence.
    """

    query: NormalizedSearchQuery
    language_code: str
    results: tuple[SearchResult, ...]
    counts: tuple[SearchKindCount, ...]

    @property
    def total_count(self) -> int:
        """Cannot drift from the results: it is derived, not stored."""
        return len(self.results)

    @property
    def is_empty(self) -> bool:
        return not self.results

    def count_for(self, kind: SearchResultKind) -> int:
        for entry in self.counts:
            if entry.kind is kind:
                return entry.count
        return 0


def build_counts(results: tuple[SearchResult, ...]) -> tuple[SearchKindCount, ...]:
    """
    Counts results per content type, in COUNT_ORDER, including zeros.

    Derived from the final result list rather than queried separately, so the
    numbers cannot disagree with what is shown.
    """
    tally = dict.fromkeys(COUNT_ORDER, 0)
    for result in results:
        tally[result.kind] += 1
    return tuple(SearchKindCount(kind=kind, count=tally[kind]) for kind in COUNT_ORDER)


def empty_response(
    *, query: NormalizedSearchQuery, language_code: str
) -> SearchResponse:
    """A response carrying no results but the full set of zero counts."""
    return SearchResponse(
        query=query,
        language_code=language_code,
        results=(),
        counts=build_counts(()),
    )
