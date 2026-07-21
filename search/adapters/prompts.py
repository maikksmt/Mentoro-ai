"""
Prompt search adapter.

The prompt text itself is the point of a prompt, so ``body`` and the
``outro`` that continues it are both indexed - both are part of the published
revision and both carry real content (every prompt in the project fills them).
Tags and linked tools stay out until the tool adapter settles how public tool
names resolve across languages.
"""
from __future__ import annotations

from prompts.models import Prompt, PromptTranslation
from search.adapters.editorial import EditorialSearchField, search_editorial
from search.query import NormalizedSearchQuery
from search.result_types import SearchMatchedField, SearchResult, SearchResultKind

PROMPT_SEARCH_FIELDS: tuple[EditorialSearchField, ...] = (
    EditorialSearchField("title", "A", SearchMatchedField.TITLE, snippet_priority=999),
    EditorialSearchField("intro", "B", SearchMatchedField.SUMMARY, snippet_priority=1),
    EditorialSearchField("body", "C", SearchMatchedField.BODY, snippet_priority=2),
    EditorialSearchField("outro", "C", SearchMatchedField.BODY, snippet_priority=3),
)


class PromptSearchAdapter:
    """Finds published prompts through PostgreSQL full-text search."""

    kind = SearchResultKind.PROMPT

    def search(
        self,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> tuple[SearchResult, ...]:
        return search_editorial(
            queryset=Prompt.objects.visible_in_language(language_code),
            translation_model=PromptTranslation,
            kind=self.kind,
            fields=PROMPT_SEARCH_FIELDS,
            query=query,
            language_code=language_code,
        )
