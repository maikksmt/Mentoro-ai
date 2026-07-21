"""Failures the global search reports to its caller."""
from __future__ import annotations

from search.result_types import SearchResultKind


class SearchExecutionError(RuntimeError):
    """
    One adapter failed or broke its contract, so the whole search failed.

    The global search is fail-closed: a partial result set would be
    indistinguishable from a complete one, and a visitor would silently see
    fewer results than exist. The message names the content type and the
    contract that broke - never database or SQL detail, which may end up in a
    response. The original exception stays reachable through ``__cause__``.
    """

    def __init__(self, kind: SearchResultKind, reason: str) -> None:
        self.kind = kind
        self.reason = reason
        super().__init__(f"{kind} search failed: {reason}")
