"""
Beta 11.11C4F: the single, read-only resolver from ``Prompt.live_author``
(Beta 11.11C4E's publish-time snapshot) to the plain display string every
public Prompt surface renders.

This is the *only* place that interprets the snapshot's shape. Every public
caller - the detail view's byline/SEO context and the prompt-list card
partial's author line - goes through :func:`resolve_prompt_live_author_display_name`
rather than re-deriving its own notion of "is this snapshot valid", so the
fail-closed contract below can never drift between call sites.

Fail-closed, never a live fallback
------------------------------------
A snapshot that is missing, malformed, or carries an unknown schema resolves
to ``""`` - never to ``prompt.author``, ``author.get_full_name()`` or
``author.username``. Beta 11.11C4E's entire point was to decouple the public
display name from the *current* account state; a fallback here would quietly
reintroduce exactly the instability that slice removed. This function never
reads ``prompt.author`` or ``prompt.author_id`` at all.

Purity
-------
No database query (``prompt.live_author`` is a plain column already present
on any loaded instance), no mutation, no exception for malformed JSON, no
active-language or request dependency, no workflow/status logic. Safe to call
any number of times against the same object.
"""
from __future__ import annotations

from typing import Any

from prompts.models import PROMPT_AUTHOR_SNAPSHOT_SCHEMA


def resolve_prompt_live_author_display_name(prompt: Any) -> str:
    """
    The public display name for ``prompt``'s author, resolved exclusively
    from its frozen ``live_author`` snapshot.

    Returns the stored ``display_name`` unchanged (only checked for
    non-emptiness via ``strip()``, never itself normalized) when the
    snapshot is a dict, its ``schema`` is exactly
    :data:`prompts.models.PROMPT_AUTHOR_SNAPSHOT_SCHEMA`, its
    ``display_name`` is a string, and that string is not empty/whitespace
    -only. Returns ``""`` for every other case: ``live_author is None``, a
    non-dict value, a wrong/missing schema, a missing/non-string
    ``display_name``, or an empty/whitespace-only ``display_name`` -
    including the deliberate "published with no author"
    ``{"schema": "prompt-author-v1", "display_name": ""}`` snapshot.

    Unknown extra keys on an otherwise valid snapshot are ignored.
    """
    snapshot = prompt.live_author

    if not isinstance(snapshot, dict):
        return ""

    if snapshot.get("schema") != PROMPT_AUTHOR_SNAPSHOT_SCHEMA:
        return ""

    display_name = snapshot.get("display_name")
    if not isinstance(display_name, str):
        return ""

    if not display_name.strip():
        return ""

    return display_name
