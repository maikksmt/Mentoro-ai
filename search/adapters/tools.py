"""
Tool search adapter.

Tools carry no editorial workflow and no published snapshot, so the search
source is the translation row itself - queried from ``ToolTranslation`` rather
than from ``Tool``, which binds the language in the base table's WHERE clause
and yields exactly one row per tool. Visibility remains
``Tool.objects.public()``, a pure ``published_at <= now`` gate.

Unlike the tool catalogue, the global search is strictly language-bound: an
English-only tool is not findable in German - not through its name, its texts,
its vendor or its categories. The catalogue's deliberate parler fallback is
untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib.postgres.aggregates import StringAgg
from django.contrib.postgres.search import SearchRank, SearchVector
from django.db.models import OuterRef, Subquery, TextField, Value
from django.db.models.functions import Coalesce

from catalog.models import CategoryTranslation, Tool, ToolTranslation
from catalog.projections import project_public_tool
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

#: Annotation holding a tool's category names in the requested language.
CATEGORY_NAMES = "category_names"

_SUMMARY_SOURCE = "short_description"
_BODY_SOURCE = "long_description"


@dataclass(frozen=True, slots=True)
class ToolSearchField:
    """One searchable source of a tool."""

    #: Identifier used for the per-field rank alias and the value lookup.
    key: str
    #: ORM path usable inside SearchVector, relative to ToolTranslation.
    sql_source: str
    weight: str
    matched_field: SearchMatchedField


TOOL_SEARCH_FIELDS: tuple[ToolSearchField, ...] = (
    ToolSearchField("name", "name", "A", SearchMatchedField.TITLE),
    ToolSearchField(_SUMMARY_SOURCE, _SUMMARY_SOURCE, "B", SearchMatchedField.SUMMARY),
    ToolSearchField(_BODY_SOURCE, _BODY_SOURCE, "C", SearchMatchedField.BODY),
    # Vendor is not translated, but a tool only reaches this query when it has
    # a real translation in the requested language - so a vendor hit can never
    # surface a tool that is absent from that language.
    ToolSearchField("vendor", "master__vendor", "B", SearchMatchedField.METADATA),
    ToolSearchField(CATEGORY_NAMES, CATEGORY_NAMES, "D", SearchMatchedField.METADATA),
)


def _category_names_subquery(language_code: str) -> Subquery:
    """
    Aggregates a tool's category names in `language_code` into one string.

    A correlated aggregate rather than a join: joining the categories M2M
    would multiply rows per tool and force a distinct() that only papers over
    the cross product. The language sits inside the subquery, so a category
    translated only in the other language contributes nothing.
    """
    return Subquery(
        CategoryTranslation.objects.filter(
            language_code=language_code,
            master__tools=OuterRef("master_id"),
        )
        .values("language_code")
        .annotate(names=StringAgg("name", delimiter=" "))
        .values("names")[:1]
    )


def _field_value(row: ToolTranslation, key: str) -> str:
    """Reads one searchable value off a result row."""
    if key == "vendor":
        return row.master.vendor or ""
    if key == CATEGORY_NAMES:
        return getattr(row, CATEGORY_NAMES, "") or ""
    return getattr(row, key) or ""


def _snippet_candidates(winner: ToolSearchField) -> tuple[str, ...]:
    """
    Which values to excerpt from, in order, for a hit in `winner`.

    A name hit shows the short description rather than repeating the heading.
    A vendor or category hit prefers the short description too - a bare brand
    or category name makes a poor excerpt - and only quotes the metadata
    itself when there is no description to show.
    """
    if winner.matched_field is SearchMatchedField.TITLE:
        return (_SUMMARY_SOURCE, _BODY_SOURCE)
    if winner.matched_field is SearchMatchedField.METADATA:
        return (_SUMMARY_SOURCE, winner.key, _BODY_SOURCE)
    return (winner.key, _SUMMARY_SOURCE, _BODY_SOURCE)


class ToolSearchAdapter:
    """Finds public tools through PostgreSQL full-text search."""

    kind = SearchResultKind.TOOL

    def search(
        self,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> tuple[SearchResult, ...]:
        """
        Returns every public tool matching `query` in `language_code`.

        Fails closed before touching the database on an unsupported language,
        an unsearchable query or a backend without full-text search. Returns
        the complete match set; capping candidates is the later search
        service's job.
        """
        config = resolve_search_config(language_code)
        search_query = build_search_query(query, config=config)
        require_postgresql()

        def vector_for(*subset: ToolSearchField) -> SearchVector:
            vector = SearchVector(
                subset[0].sql_source, config=config, weight=subset[0].weight
            )
            for field in subset[1:]:
                vector = vector + SearchVector(
                    field.sql_source, config=config, weight=field.weight
                )
            return vector

        def rank_for(*subset: ToolSearchField) -> SearchRank:
            return SearchRank(
                vector_for(*subset),
                search_query,
                normalization=SEARCH_RANK_NORMALIZATION,
            )

        # Querying the translation rows binds the language in the base table
        # and yields exactly one row per tool, so no distinct() is needed and
        # no parler fallback can reach the vector. Visibility stays with
        # Tool.objects.public().
        rows = (
            ToolTranslation.objects.filter(
                language_code=language_code,
                master_id__in=Tool.objects.public().values("pk"),
            )
            .select_related("master")
            .annotate(
                **{
                    CATEGORY_NAMES: Coalesce(
                        _category_names_subquery(language_code),
                        Value(""),
                        output_field=TextField(),
                    )
                }
            )
        )

        # Filtering runs on the @@ match operator, never on rank > 0: ts_rank
        # awards partial credit for a query the document does not satisfy.
        rows = rows.annotate(
            search_vector=vector_for(*TOOL_SEARCH_FIELDS),
            rank=rank_for(*TOOL_SEARCH_FIELDS),
            **{f"rank_{field.key}": rank_for(field) for field in TOOL_SEARCH_FIELDS},
        ).filter(search_vector=search_query)

        results = [
            self._to_result(row, query=query, language_code=language_code)
            for row in rows
        ]
        return sort_search_results(results, query=query)

    def _to_result(
        self,
        row: ToolTranslation,
        *,
        query: NormalizedSearchQuery,
        language_code: str,
    ) -> SearchResult:
        projection = project_public_tool(row, language_code=language_code)
        winner = self._strongest_field(row)
        return SearchResult(
            kind=self.kind,
            object_id=projection.object_id,
            title=projection.title,
            summary=build_search_snippet(
                self._snippet_source(row, winner), query=query
            ),
            url=projection.url,
            language_code=language_code,
            published_at=projection.published_at,
            updated_at=projection.updated_at,
            rank=row.rank,
            matched_field=winner.matched_field,
        )

    @staticmethod
    def _strongest_field(row: ToolTranslation) -> ToolSearchField:
        """
        The field that contributed most to the match.

        Uses PostgreSQL's per-field ranks rather than a Python substring test:
        a stemmed hit has no literal substring to find. Ties fall to
        declaration order - name, short description, long description, vendor,
        categories - because max() keeps the first maximum.
        """
        return max(
            TOOL_SEARCH_FIELDS, key=lambda field: getattr(row, f"rank_{field.key}")
        )

    @staticmethod
    def _snippet_source(row: ToolTranslation, winner: ToolSearchField) -> str:
        for key in _snippet_candidates(winner):
            value = _field_value(row, key)
            if value:
                return value
        return ""
