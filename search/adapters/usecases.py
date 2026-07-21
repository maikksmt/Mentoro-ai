"""
Use case search adapter.

``persona`` is deliberately not indexed. It would be a strong search
dimension - "freelancer", "teacher" - but UseCase.LIVE_SNAPSHOT_FIELDS does
not include it, so it has no published representation at all: the public
projection can only ever return "" for it, and reading the current
translation instead would search draft text. Indexing it needs the model to
snapshot it first.

Linked tools stay out until the tool adapter settles how public tool names
resolve across languages.
"""
from __future__ import annotations

from search.adapters.editorial import EditorialSearchField, search_editorial
from search.query import NormalizedSearchQuery
from search.result_types import SearchMatchedField, SearchResult, SearchResultKind
from usecases.models import UseCase, UseCaseTranslation

USE_CASE_SEARCH_FIELDS: tuple[EditorialSearchField, ...] = (
    EditorialSearchField("title", "A", SearchMatchedField.TITLE, snippet_priority=999),
    EditorialSearchField("intro", "B", SearchMatchedField.SUMMARY, snippet_priority=1),
    EditorialSearchField("body", "C", SearchMatchedField.BODY, snippet_priority=2),
    EditorialSearchField("outro", "C", SearchMatchedField.BODY, snippet_priority=3),
)


class UseCaseSearchAdapter:
    """Finds published use cases through PostgreSQL full-text search."""

    kind = SearchResultKind.USE_CASE

    def search(
        self,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> tuple[SearchResult, ...]:
        # UseCaseQuerySet.visible_in_language() builds on published(), not on
        # the broader visible_on_site() that guides and prompts use. Search
        # mirrors whatever the model decides; it never defines its own rule.
        return search_editorial(
            queryset=UseCase.objects.visible_in_language(language_code),
            translation_model=UseCaseTranslation,
            kind=self.kind,
            fields=USE_CASE_SEARCH_FIELDS,
            query=query,
            language_code=language_code,
        )
