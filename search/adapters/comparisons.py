"""
Comparison search adapter.

Compared tool names are deliberately not indexed yet, even though
"ChatGPT vs Claude" is an obvious search intent. Reaching them means joining
the tools M2M, restricting to the requested language's tool translation and
excluding tools whose published_at lies in the future - and the public
semantics of a tool name (parler falls back across languages for tools by
design) is exactly what the tool adapter still has to decide. Binding it here
first would commit that decision from the wrong side.

ComparisonToolEntry's label/summary/pros/cons/special stay out for the same
reason, plus a second translation join for two comparisons.

This adapter is independent of the language-hardened comparison list search in
compare/views.py; that view is untouched.
"""
from __future__ import annotations

from compare.models import Comparison, ComparisonTranslation
from search.adapters.editorial import EditorialSearchField, search_editorial
from search.query import NormalizedSearchQuery
from search.result_types import SearchMatchedField, SearchResult, SearchResultKind

COMPARISON_SEARCH_FIELDS: tuple[EditorialSearchField, ...] = (
    EditorialSearchField("title", "A", SearchMatchedField.TITLE, snippet_priority=999),
    EditorialSearchField("intro", "B", SearchMatchedField.SUMMARY, snippet_priority=1),
    EditorialSearchField("body", "C", SearchMatchedField.BODY, snippet_priority=2),
)


class ComparisonSearchAdapter:
    """Finds published comparisons through PostgreSQL full-text search."""

    kind = SearchResultKind.COMPARISON

    def search(
        self,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> tuple[SearchResult, ...]:
        # ComparisonQuerySet.visible_in_language() builds on visible_on_site()
        # since Beta 11.9, matching guides and prompts. Search mirrors
        # whatever the model decides; it never defines its own rule.
        return search_editorial(
            queryset=Comparison.objects.visible_in_language(language_code),
            translation_model=ComparisonTranslation,
            kind=self.kind,
            fields=COMPARISON_SEARCH_FIELDS,
            query=query,
            language_code=language_code,
        )
