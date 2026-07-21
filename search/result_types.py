"""
The result contract shared by every search adapter, the search service and
the result template.

Pure data: no model instances, no querysets, no lazy translations, no
HTML-safe strings.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SearchResultKind(StrEnum):
    """
    The globally searchable content types.

    Values match the kind strings already used across the project
    (``to_teaser_item``, ``GuideItem.kind``, the ``btn-*`` card classes), so
    templates, CSS intents and logs need no translation table.
    """

    TOOL = "tool"
    GUIDE = "guide"
    PROMPT = "prompt"
    USE_CASE = "usecase"
    COMPARISON = "comparison"


class SearchMatchedField(StrEnum):
    """
    Which kind of field produced the match.

    ``METADATA`` covers everything that is not the result's own running text -
    category, tag, persona, vendor, linked tool name - so app-specific field
    names stay out of this shared contract.
    """

    TITLE = "title"
    SUMMARY = "summary"
    BODY = "body"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """
    One search hit, ready for ranking and template rendering.

    ``summary`` is plain text, ``url`` is an internal public detail URL (never
    an external tool website), and ``rank`` is the database relevance score
    exactly as returned - adapters must not rescale it against their own
    maximum, which would lift a weak hit from a small content type above a
    strong hit from a large one.
    """

    kind: SearchResultKind
    object_id: int
    title: str
    summary: str
    url: str
    language_code: str
    published_at: datetime | None
    updated_at: datetime | None
    rank: float
    matched_field: SearchMatchedField

    def __post_init__(self) -> None:
        if self.object_id <= 0:
            raise ValueError(f"object_id must be positive, got {self.object_id!r}")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if not self.url.strip():
            raise ValueError("url must not be empty")
        if not self.language_code.strip():
            raise ValueError("language_code must not be empty")
        if not math.isfinite(self.rank):
            raise ValueError(f"rank must be a finite number, got {self.rank!r}")
        if self.rank < 0:
            raise ValueError(f"rank must not be negative, got {self.rank!r}")
