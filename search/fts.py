"""
Shared PostgreSQL full-text search configuration.

Every adapter resolves its text search configuration and builds its query
through this module, so ranks stay comparable across content types.
"""
from __future__ import annotations

from django.contrib.postgres.search import SearchQuery
from django.db import connection

from search.query import NormalizedSearchQuery

#: Project language -> PostgreSQL text search configuration. Both are shipped
#: with PostgreSQL; no extension is required.
POSTGRES_SEARCH_CONFIG_BY_LANGUAGE = {
    "en": "english",
    "de": "german",
}

#: ts_rank normalization, identical for every adapter.
#:
#: Bit 32 divides the rank by itself plus one, mapping any raw score into
#: [0, 1) without penalising document length. That bound is what makes ranks
#: from different content types comparable in one mixed result list, and it
#: removes any need for an adapter to rescale against its own maximum - which
#: would lift a weak hit from a small content type above a strong hit from a
#: large one. Length-dividing options (1, 2, 16) were rejected because they
#: would systematically disadvantage long guides against short tool blurbs.
SEARCH_RANK_NORMALIZATION = 32

#: websearch_to_tsquery: quoted phrases and leading "-" exclusions, and no
#: syntax error for any input. No hand-written query parser.
SEARCH_QUERY_TYPE = "websearch"


class UnsupportedSearchLanguage(ValueError):
    """Raised for a language with no PostgreSQL text search configuration."""


class SearchBackendUnavailable(RuntimeError):
    """Raised when the active database cannot serve full-text search."""


def resolve_search_config(language_code: str) -> str:
    """
    Returns the PostgreSQL text search configuration for `language_code`.

    Fails closed: an unknown language raises rather than silently falling back
    to 'english', which would stem German text with English rules and quietly
    return wrong results.
    """
    try:
        return POSTGRES_SEARCH_CONFIG_BY_LANGUAGE[language_code]
    except KeyError:
        supported = ", ".join(sorted(POSTGRES_SEARCH_CONFIG_BY_LANGUAGE))
        raise UnsupportedSearchLanguage(
            f"no search configuration for language {language_code!r} "
            f"(supported: {supported})"
        ) from None


def require_postgresql() -> None:
    """
    Fails closed on any non-PostgreSQL backend.

    There is deliberately no icontains fallback: it would silently degrade
    ranking, stemming and weighting into substring matching, and nothing in
    the result would say so.
    """
    if connection.vendor != "postgresql":
        raise SearchBackendUnavailable(
            f"full-text search requires PostgreSQL, got {connection.vendor!r}"
        )


def build_search_query(query: NormalizedSearchQuery, *, config: str) -> SearchQuery:
    """Builds the tsquery for an already-validated, searchable query."""
    if not query.is_searchable:
        raise ValueError(
            f"cannot build a search query for an unsearchable query "
            f"(issue={query.issue!r})"
        )
    return SearchQuery(query.value, config=config, search_type=SEARCH_QUERY_TYPE)
