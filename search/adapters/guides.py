"""
Guide search adapter.

Searches only what a visitor can actually read on the public guide detail page
in the requested language: the published ``live_i18n`` revision, falling back
to the current translation only where no snapshot exists at all. A draft
title, intro, body or slug is never a search source and never reaches a
result.

Linked tools and categories stay out - no guide in the project carries either,
and the public semantics of a tool name belongs to the tool adapter.
"""
from __future__ import annotations

from guides.models import Guide, GuideTranslation
from search.adapters.editorial import EditorialSearchField, search_editorial
from search.query import NormalizedSearchQuery
from search.result_types import SearchMatchedField, SearchResult, SearchResultKind

GUIDE_SEARCH_FIELDS: tuple[EditorialSearchField, ...] = (
    EditorialSearchField("title", "A", SearchMatchedField.TITLE, snippet_priority=999),
    EditorialSearchField("intro", "B", SearchMatchedField.SUMMARY, snippet_priority=1),
    EditorialSearchField("body", "C", SearchMatchedField.BODY, snippet_priority=2),
)


class GuideSearchAdapter:
    """Finds published guides through PostgreSQL full-text search."""

    kind = SearchResultKind.GUIDE

    def search(
        self,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> tuple[SearchResult, ...]:
        return search_editorial(
            queryset=Guide.objects.visible_in_language(language_code),
            translation_model=GuideTranslation,
            kind=self.kind,
            fields=GUIDE_SEARCH_FIELDS,
            query=query,
            language_code=language_code,
        )
