"""
Shared full-text search for the editorial content types.

Guides, prompts, use cases and comparisons differ only in which public fields
they expose and how heavily each weighs. Everything else - the published
``live_i18n`` revision as the sole search source, the language binding, the
weighted vector, the match predicate, the rank and the result projection - is
identical, and lives here once.

Each adapter names its own public queryset rather than inheriting one: that
queryset is the visibility boundary, and it belongs where a reader looking at
the adapter will see it.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib.postgres.search import SearchRank, SearchVector
from django.db.models import Model, QuerySet

from core.projections import (
    project_public_content,
    public_content_value,
    public_content_value_expression,
)
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

#: A field never used as a snippet source. Titles are already shown as the
#: result heading, so repeating them in the excerpt says nothing.
NEVER_AS_SNIPPET = 999


@dataclass(frozen=True, slots=True)
class EditorialSearchField:
    """One searchable public field of an editorial model."""

    #: Name of the translated field, resolved through the public projection.
    public_field: str
    #: PostgreSQL text search weight: A titles, B summaries, C body text.
    weight: str
    #: What a hit in this field means for the result.
    matched_field: SearchMatchedField
    #: Preference as a snippet source when the hit itself offers none.
    #: Lower wins; NEVER_AS_SNIPPET excludes the field entirely.
    snippet_priority: int


def _rank_alias(field: EditorialSearchField) -> str:
    return f"rank_{field.public_field}"


def search_editorial(
    *,
    queryset: QuerySet,
    translation_model: type[Model],
    kind: SearchResultKind,
    fields: tuple[EditorialSearchField, ...],
    query: NormalizedSearchQuery,
    language_code: str,
    summary_field: str = "intro",
) -> tuple[SearchResult, ...]:
    """
    Runs the shared editorial search over an already public-filtered
    `queryset`.

    Fails closed before touching the database on an unsupported language, an
    unsearchable query or a backend without full-text search. Returns the
    complete match set - capping candidates is the later search service's
    job, and a silent cap here would make its result count wrong.
    """
    config = resolve_search_config(language_code)
    search_query = build_search_query(query, config=config)
    require_postgresql()

    public_expressions = {
        field.public_field: public_content_value_expression(
            translation_model, field.public_field, language_code=language_code
        )
        for field in fields
    }

    def vector_for(*subset: EditorialSearchField) -> SearchVector:
        vector = SearchVector(
            public_expressions[subset[0].public_field],
            config=config,
            weight=subset[0].weight,
        )
        for field in subset[1:]:
            vector = vector + SearchVector(
                public_expressions[field.public_field],
                config=config,
                weight=field.weight,
            )
        return vector

    def rank_for(*subset: EditorialSearchField) -> SearchRank:
        return SearchRank(
            vector_for(*subset), search_query, normalization=SEARCH_RANK_NORMALIZATION
        )

    # Filtering runs on the @@ match operator, never on rank > 0: ts_rank
    # awards partial credit for a query the document does not actually
    # satisfy, so "machine bicycle" would score above zero on a document
    # containing only "machine" and leak in as a false hit.
    annotations = {
        "search_vector": vector_for(*fields),
        "rank": rank_for(*fields),
        **{_rank_alias(field): rank_for(field) for field in fields},
    }
    matches = queryset.annotate(**annotations).filter(search_vector=search_query)

    results = [
        _to_result(
            obj,
            kind=kind,
            fields=fields,
            query=query,
            language_code=language_code,
            summary_field=summary_field,
        )
        for obj in matches
    ]
    return sort_search_results(results, query=query)


def _strongest_field(
    obj: Model, fields: tuple[EditorialSearchField, ...]
) -> EditorialSearchField:
    """
    The field that contributed most to the match.

    Uses PostgreSQL's per-field ranks rather than a Python substring test: a
    stemmed hit ("Anleitungen" matching "Anleitung") has no literal substring
    to find, and a query whose terms are spread over several fields satisfies
    none of them alone. Ties fall to declaration order, because max() keeps
    the first maximum - so a title outranks a summary outranks a body.
    """
    return max(fields, key=lambda field: getattr(obj, _rank_alias(field)))


def _snippet_source(
    public_values: dict[str, str],
    fields: tuple[EditorialSearchField, ...],
    winner: EditorialSearchField,
) -> str:
    """
    Picks the text to excerpt.

    The winning field itself, unless it is one that never makes a useful
    excerpt (a title); then the remaining fields in snippet_priority order,
    skipping empty ones.
    """
    if winner.snippet_priority != NEVER_AS_SNIPPET and public_values[winner.public_field]:
        return public_values[winner.public_field]

    for field in sorted(fields, key=lambda f: f.snippet_priority):
        if field.snippet_priority == NEVER_AS_SNIPPET:
            continue
        if public_values[field.public_field]:
            return public_values[field.public_field]
    return ""


def _to_result(
    obj: Model,
    *,
    kind: SearchResultKind,
    fields: tuple[EditorialSearchField, ...],
    query: NormalizedSearchQuery,
    language_code: str,
    summary_field: str,
) -> SearchResult:
    projection = project_public_content(
        obj, kind, language_code=language_code, summary_field=summary_field
    )
    # Extra fields such as `outro` are not part of PublicContentProjection -
    # only search needs them, so they are read through the same public API
    # rather than widening a type shared with the teasers.
    public_values = {
        field.public_field: public_content_value(
            obj, field.public_field, language_code=language_code
        )
        for field in fields
    }
    winner = _strongest_field(obj, fields)
    return SearchResult(
        kind=kind,
        object_id=projection.object_id,
        title=projection.title,
        summary=build_search_snippet(
            _snippet_source(public_values, fields, winner), query=query
        ),
        url=projection.url,
        language_code=language_code,
        published_at=projection.published_at,
        updated_at=projection.updated_at,
        rank=obj.rank,
        matched_field=winner.matched_field,
    )
