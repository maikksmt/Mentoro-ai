"""
Use case search adapter.

``persona`` is deliberately not indexed. Beta 11.7 added it to
UseCase.LIVE_SNAPSHOT_FIELDS, so it now does have a published
representation - but only for objects published since then, and indexing a
new field changes result ranking and snippets. That is a search decision,
not a visibility one, and stays out of this slice.

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
        # UseCaseQuerySet.visible_in_language() builds on visible_on_site()
        # since Beta 11.7, matching guides and prompts. Search mirrors
        # whatever the model decides; it never defines its own rule.
        return search_editorial(
            queryset=UseCase.objects.visible_in_language(language_code),
            translation_model=UseCaseTranslation,
            kind=self.kind,
            fields=USE_CASE_SEARCH_FIELDS,
            query=query,
            language_code=language_code,
        )
