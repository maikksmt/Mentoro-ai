"""
Plain-text snippet extraction for search results.

Output is ordinary text, never marked safe: the template autoescapes it, so a
snippet can carry no markup and no highlighting into the page.
"""
from __future__ import annotations

import re
from html import unescape

from django.utils.html import strip_tags

from search.query import NormalizedSearchQuery, normalize_text

DEFAULT_SNIPPET_LENGTH = 200

_ELLIPSIS = "…"


def _visible_text(source: str) -> str:
    """
    Reduces rich text to the words a reader actually sees.

    Tags are stripped before entities are decoded, so escaped markup in the
    body stays literal text instead of turning into tags that are then
    removed. A consequence worth knowing: attribute values disappear with
    their tags, so a query occurring only in an ``href`` or ``class`` is
    correctly not treated as a visible hit.
    """
    return normalize_text(unescape(strip_tags(source)))


def _window_bounds(text_length: int, start: int, max_length: int) -> tuple[int, int]:
    """
    Fits a window starting near `start` into `max_length`, counting the
    ellipsis characters that the window itself makes necessary.

    Converges in at most two passes: widening the window to the end of the
    text can free the leading budget, which may shift the start once.
    """
    for _ in range(2):
        lead = len(_ELLIPSIS) if start > 0 else 0
        budget = max(0, max_length - lead)
        end = start + budget
        if end >= text_length:
            end = text_length
            shifted = max(0, end - budget)
            if shifted != start:
                start = shifted
                continue
        else:
            end = start + max(0, budget - len(_ELLIPSIS))
        break
    return start, end


def _snap_to_word_bounds(
    text: str,
    start: int,
    end: int,
    protected: tuple[int, int] | None,
) -> tuple[int, int]:
    """
    Pulls the cut points onto whitespace so the snippet does not begin or end
    mid-word. Only ever shrinks the window, so the length budget still holds,
    and never trims into `protected` (the matched query span).
    """
    keep_from, keep_to = protected if protected else (start, end)

    if start > 0 and not text[start - 1].isspace():
        space = text.find(" ", start, keep_from if keep_from > start else end)
        if space != -1 and space + 1 <= keep_from:
            start = space + 1

    if end < len(text) and not text[end].isspace():
        space = text.rfind(" ", keep_to if keep_to < end else start, end)
        if space != -1 and space >= keep_to:
            end = space

    return start, end


def build_search_snippet(
    source: str | None,
    *,
    query: NormalizedSearchQuery,
    max_length: int = DEFAULT_SNIPPET_LENGTH,
) -> str:
    """
    Builds a plain-text excerpt of `source` centred on the first occurrence of
    `query`, at most `max_length` characters including ellipses.

    Falls back to the beginning of the text when the query does not occur
    literally - a result matched through a category, a tool name or a stemmed
    form has no substring to centre on.

    Matching uses a case-insensitive regular expression rather than
    ``casefold()`` on purpose: casefolding can change a string's length (``ß``
    becomes ``ss``), which would misalign every offset computed afterwards.

    Raises ValueError for an unsearchable query or a non-positive
    `max_length`, matching ``determine_match_tier``: the search pipeline stops
    before any adapter runs when a query is unusable, so reaching this point
    with one is a programming error.
    """
    if max_length < 1:
        raise ValueError(f"max_length must be positive, got {max_length!r}")
    if not query.is_searchable:
        raise ValueError(
            f"cannot build a snippet for an unsearchable query "
            f"(issue={query.issue!r})"
        )
    if not source:
        return ""

    text = _visible_text(source)
    if not text:
        return ""
    if len(text) <= max_length:
        return text

    match = re.search(re.escape(query.value), text, re.IGNORECASE)
    if match:
        slack = max(0, max_length - (match.end() - match.start()))
        start = max(0, match.start() - slack // 2)
        protected: tuple[int, int] | None = (match.start(), match.end())
    else:
        start = 0
        protected = None

    start, end = _window_bounds(len(text), start, max_length)
    start, end = _snap_to_word_bounds(text, start, end, protected)

    lead = _ELLIPSIS if start > 0 else ""
    trail = _ELLIPSIS if end < len(text) else ""
    return (lead + text[start:end].strip() + trail)[:max_length]
