"""
The global search application layer.

Normalises the raw query once, runs every adapter, verifies what they return
and sorts everything together. Knows nothing about requests, templates or
pagination - a view calls this and renders the response.
"""
from __future__ import annotations

from typing import Iterable

from search.adapters.base import SearchAdapter
from search.exceptions import SearchExecutionError
from search.fts import resolve_search_config
from search.query import NormalizedSearchQuery, normalize_search_query
from search.ranking import sort_search_results
from search.registry import SEARCH_ADAPTERS
from search.responses import SearchResponse, build_counts, empty_response
from search.result_types import SearchResult


def search_site(
    *,
    raw_query: str | None,
    language_code: str,
    adapters: Iterable[SearchAdapter] = SEARCH_ADAPTERS,
) -> SearchResponse:
    """
    Searches every content type for `raw_query` in `language_code`.

    An unusable query short-circuits before anything else: no adapter runs and
    no query reaches the database. That ordering is deliberate - a visitor who
    typed one character should get the same empty answer whatever else is
    wrong, and the returned query carries the reason so a view can say which
    rule it broke.

    A searchable query then validates the language, runs the adapters in
    registry order, and sorts all results together exactly once.

    Raises SearchExecutionError if any adapter fails or breaks its contract.
    Nothing partial is ever returned: the results already collected are
    discarded and the remaining adapters do not run.
    """
    query = normalize_search_query(raw_query)
    if not query.is_searchable:
        return empty_response(query=query, language_code=language_code)

    # Fails closed on an unsupported language, before any adapter runs.
    resolve_search_config(language_code)

    collected: list[SearchResult] = []
    seen: set[tuple[str, int]] = set()
    for adapter in adapters:
        results = _run_adapter(adapter, query=query, language_code=language_code)
        _validate(adapter, results, language_code=language_code, seen=seen)
        collected.extend(results)

    ordered = sort_search_results(collected, query=query)
    return SearchResponse(
        query=query,
        language_code=language_code,
        results=ordered,
        counts=build_counts(ordered),
    )


def _run_adapter(
    adapter: SearchAdapter,
    *,
    query: NormalizedSearchQuery,
    language_code: str,
) -> tuple[SearchResult, ...]:
    """
    Runs one adapter, turning any failure into a SearchExecutionError that
    names the content type.

    The original exception is chained rather than described, so its text -
    which may carry SQL - cannot reach a response. KeyboardInterrupt and
    SystemExit derive from BaseException and pass through untouched.
    """
    try:
        return adapter.search(query=query, language_code=language_code)
    except Exception as exc:
        raise SearchExecutionError(adapter.kind, "adapter raised") from exc


def _validate(
    adapter: SearchAdapter,
    results: object,
    *,
    language_code: str,
    seen: set[tuple[str, int]],
) -> None:
    """
    Checks the boundary between an adapter and the service.

    Only what the service can verify from the results themselves - no model
    lookups, and no second opinion on visibility, which is the adapter's
    responsibility alone.
    """

    def fail(reason: str) -> None:
        raise SearchExecutionError(adapter.kind, reason)

    if not isinstance(results, tuple):
        fail(f"returned {type(results).__name__}, expected a tuple")

    for result in results:
        if not isinstance(result, SearchResult):
            fail(f"returned a {type(result).__name__}, expected SearchResult")
        if result.kind is not adapter.kind:
            fail(f"returned a {result.kind} result")
        if result.language_code != language_code:
            fail(f"returned a {result.language_code!r} result, expected {language_code!r}")
        if not _is_internal_url(result.url):
            fail(f"returned a non-internal url for object {result.object_id}")

        identity = (str(result.kind), result.object_id)
        if identity in seen:
            fail(f"returned a duplicate of object {result.object_id}")
        seen.add(identity)


def _is_internal_url(url: str) -> bool:
    """
    A usable internal path: rooted, not a placeholder, not pointing off-site.

    Protocol-relative URLs ("//example.test/x") start with a slash but leave
    the site, so a leading-slash check alone would let them through.
    """
    if not url or url == "#":
        return False
    if url.startswith("//"):
        return False
    if "://" in url:
        return False
    return url.startswith("/")
