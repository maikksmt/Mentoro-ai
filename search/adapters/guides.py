"""
Guide search adapter.

Searches only what a visitor can actually read on the public guide detail page
in the requested language: the published ``live_i18n`` revision, falling back
to the current translation only where no snapshot exists. A draft title, intro,
body or slug is never a search source and never reaches a result.
"""
from __future__ import annotations

from django.contrib.postgres.search import SearchRank, SearchVector

from core.projections import (
    project_public_content,
    public_content_value_expression,
)
from guides.models import Guide, GuideTranslation
from search.fts import (
    SEARCH_RANK_NORMALIZATION,
    build_search_query,
    require_postgresql,
    resolve_search_config,
)
from search.query import NormalizedSearchQuery
from search.ranking import sort_search_results
from search.result_types import SearchMatchedField, SearchResult, SearchResultKind
from search.snippets import build_search_snippet

#: Public field -> full-text weight. A/B/C follow PostgreSQL's default weights
#: (1.0 / 0.4 / 0.2). D stays unused for now and is reserved for metadata such
#: as linked tool names.
_TITLE_FIELD = "title"
_SUMMARY_FIELD = "intro"
_BODY_FIELD = "body"

_FIELD_WEIGHTS = (
    (_TITLE_FIELD, "A"),
    (_SUMMARY_FIELD, "B"),
    (_BODY_FIELD, "C"),
)

#: Which field wins when several match. Title is the strongest statement about
#: what a guide is; the body is the weakest.
_MATCHED_FIELD_PRIORITY = (
    ("title_rank", SearchMatchedField.TITLE),
    ("summary_rank", SearchMatchedField.SUMMARY),
    ("body_rank", SearchMatchedField.BODY),
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
        """
        Returns every public guide matching `query` in `language_code`.

        Fails closed before touching the database on an unsearchable query, an
        unsupported language or a non-PostgreSQL backend. Returns the complete
        match set - capping candidates is the later search service's job, and a
        silent cap here would make its result count wrong.
        """
        config = resolve_search_config(language_code)
        search_query = build_search_query(query, config=config)
        require_postgresql()

        public = {
            field: public_content_value_expression(
                GuideTranslation, field, language_code=language_code
            )
            for field, _weight in _FIELD_WEIGHTS
        }

        weights = dict(_FIELD_WEIGHTS)

        def vector_for(*fields: str) -> SearchVector:
            vector = SearchVector(
                public[fields[0]], config=config, weight=weights[fields[0]]
            )
            for field in fields[1:]:
                vector = vector + SearchVector(
                    public[field], config=config, weight=weights[field]
                )
            return vector

        def rank_for(*fields: str) -> SearchRank:
            return SearchRank(
                vector_for(*fields),
                search_query,
                normalization=SEARCH_RANK_NORMALIZATION,
            )

        # Filtering happens on the @@ match operator, never on rank > 0:
        # ts_rank awards partial credit for a query the document does not
        # actually satisfy, so "machine bicycle" would score above zero on a
        # document containing only "machine" and leak in as a false hit.
        queryset = (
            Guide.objects.visible_in_language(language_code)
            .annotate(
                search_vector=vector_for(_TITLE_FIELD, _SUMMARY_FIELD, _BODY_FIELD),
                rank=rank_for(_TITLE_FIELD, _SUMMARY_FIELD, _BODY_FIELD),
                title_rank=rank_for(_TITLE_FIELD),
                summary_rank=rank_for(_SUMMARY_FIELD),
                body_rank=rank_for(_BODY_FIELD),
            )
            .filter(search_vector=search_query)
        )

        results = [
            self._to_result(guide, query=query, language_code=language_code)
            for guide in queryset
        ]
        return sort_search_results(results, query=query)

    def _to_result(
        self,
        guide: Guide,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> SearchResult:
        projection = project_public_content(
            guide, self.kind, language_code=language_code, summary_field=_SUMMARY_FIELD
        )
        matched_field = self._matched_field(guide)
        return SearchResult(
            kind=self.kind,
            object_id=projection.object_id,
            title=projection.title,
            summary=build_search_snippet(
                self._snippet_source(projection, matched_field), query=query
            ),
            url=projection.url,
            language_code=language_code,
            published_at=projection.published_at,
            updated_at=projection.updated_at,
            rank=guide.rank,
            matched_field=matched_field,
        )

    @staticmethod
    def _matched_field(guide: Guide) -> SearchMatchedField:
        """
        Reports the field that contributed most to the match.

        Uses PostgreSQL's per-field ranks rather than a Python substring test:
        a stemmed hit ("Anleitungen" matching "Anleitung") has no literal
        substring to find. Ranking by contribution rather than by mere
        presence also handles a query whose terms are spread across several
        fields, where no single field satisfies it alone. Ties fall back to
        _MATCHED_FIELD_PRIORITY order, because max() keeps the first maximum.
        """
        best_rank, best_field = max(
            ((getattr(guide, attribute), field) for attribute, field in _MATCHED_FIELD_PRIORITY),
            key=lambda pair: pair[0],
        )
        return best_field if best_rank > 0 else SearchMatchedField.BODY

    @staticmethod
    def _snippet_source(projection, matched_field: SearchMatchedField) -> str:
        """Excerpts the field that matched; a title hit reads better with the
        intro, which summarises the guide, than with the start of its body."""
        if matched_field is SearchMatchedField.BODY:
            return projection.body or projection.summary
        return projection.summary or projection.body
