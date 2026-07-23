"""
Beta 11.9: the public rendering contract for a comparison's tool entries.

Why this module exists
----------------------
``ComparisonDetailView`` used to hand the template
``obj.tool_entries.select_related("tool").all()`` - the live database rows -
and the template then read ``entry.label``/``entry.summary``/... straight off
the parler descriptors. Every unpublished edit to an entry (text, order, a
swapped tool, an added or removed entry) was therefore public the moment it
was saved. That was harmless only because a comparison used to drop offline
entirely the instant an edit moved it out of ``published``; Beta 11.9 keeps
it online, so the entries need a real published source first.

The published source is ``Comparison.live_entries`` (written by
``Comparison.on_after_publish()``). This module turns it back into the small
view models the detail template consumes, in one explicitly requested
language.

Three states, mirroring ``core/projections.py``'s A/B/C contract:

* **A** - ``live_entries`` is a list: authoritative. Entry text comes from
  the snapshot's own ``translations[language_code]``; a language missing
  from an entry's snapshot yields empty strings for that entry rather than
  another language's text.
* **C** - ``live_entries`` is ``None``: a record published before Beta 11.9,
  which never got a snapshot. The live rows stand in, exactly as they did
  before this slice. ``ComparisonQuerySet.visible_on_site()`` refuses to
  keep such a record public once it leaves ``published``, so this branch can
  only ever serve a genuinely published state.
* There is no B: a comparison with entries published in other languages only
  is already excluded by ``visible_in_language()``.

There is deliberately no fallback *from* A *to* the live rows: once a
snapshot exists it is the only source, even if it is empty.

Tool resolution (Beta 11.9A)
-----------------------------
The snapshot stores only a stable ``tool_id`` (see ``ENTRY_LIVE_FIELDS`` and
``Comparison.build_live_entries()``); the tool's display data is read live
through ``Tool.objects.public()`` at render time, never frozen. Tool has no
editorial workflow of its own - ``published_at <= now`` is its only public
gate - so an entry whose tool is deleted, not yet public, or withdrawn is
skipped, independent of the entry snapshot itself.

Bulk surfaces (Beta 11.9C/11.9D)
---------------------------------
The category filter (``live_tool_ids_for_comparisons()``) and the list
page's card badges (``public_tools_for_comparisons()``) both need every
visible comparison's tool membership at once rather than one comparison's
full entry content - the former as an unordered ID set for category lookups,
the latter as an ordered list of resolved Tool objects for rendering. Both
honour the identical State-A/State-C boundary as ``public_tool_entries()``
above, just without materialising ``PublicToolEntry`` objects or entry text.
"""
from __future__ import annotations

from dataclasses import dataclass

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin

from .models import ENTRY_LIVE_FIELDS, ComparisonToolEntry


@dataclass(frozen=True)
class PublicToolEntry:
    """One already-resolved comparison entry, as the detail template reads it."""

    tool: Tool
    label: str
    summary: str
    pros: str
    cons: str
    special: str


def _entry_from_snapshot(item: dict, tool: Tool, language_code: str) -> PublicToolEntry:
    translations = item.get("translations") or {}
    values = translations.get(language_code)
    if not isinstance(values, dict):
        # Published, but not in this language: empty rather than another
        # language's text. Never falls through to the draft row.
        values = {}
    return PublicToolEntry(
        tool=tool,
        **{field: (values.get(field) or "") for field in ENTRY_LIVE_FIELDS},
    )


def _entries_from_snapshot(snapshot: list, language_code: str) -> list[PublicToolEntry]:
    """
    State A. Order comes from the snapshot list itself, which
    ``build_live_entries()`` wrote in published ``(position, pk)`` order -
    the current ``position`` values cannot reorder a published comparison.

    Tools are resolved in one query, through the same public Tool contract
    (``Tool.objects.public()``, ``published_at <= now``) every other public
    surface uses - Tool has no editorial workflow of its own, so this is the
    only gate it has. An entry whose tool row no longer exists, or whose
    tool is not (or not yet) public, is skipped rather than rendered
    half-empty; the tool FK is ``on_delete=CASCADE``, so a missing row only
    happens for a tool deleted after the snapshot was taken. Beta 11.9A:
    previously this read the unfiltered ``Tool.objects.in_bulk()``, so an
    entry could render a tool that was never, not yet, or no longer public.
    """
    tool_ids = [
        item.get("tool_id")
        for item in snapshot
        if isinstance(item, dict) and item.get("tool_id") is not None
    ]
    tools_by_id = Tool.objects.public().in_bulk(tool_ids)

    entries: list[PublicToolEntry] = []
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        tool = tools_by_id.get(item.get("tool_id"))
        if tool is None:
            continue
        entries.append(_entry_from_snapshot(item, tool, language_code))
    return entries


def _entries_from_live_rows(comparison, language_code: str) -> list[PublicToolEntry]:
    """
    State C, for records predating Beta 11.9 only.

    Reads the same rows, in the same order, that the pre-Beta-11.9 detail
    view read, so such a comparison renders byte-identically to before. It
    is reachable only while the comparison is strictly ``published`` (see
    ``ComparisonQuerySet.visible_on_site()``), i.e. only while those rows
    *are* the published state.
    """
    entries: list[PublicToolEntry] = []
    for entry in comparison.tool_entries.select_related("tool").order_by("position", "pk"):
        entries.append(
            PublicToolEntry(
                tool=entry.tool,
                **{
                    field: (
                        entry.safe_translation_getter(
                            field, language_code=language_code, any_language=False
                        )
                        or ""
                    )
                    for field in ENTRY_LIVE_FIELDS
                },
            )
        )
    return entries


def public_tool_entries(comparison, language_code: str) -> list[PublicToolEntry]:
    """
    The comparison's publicly visible tool entries in ``language_code``.

    Never reads a draft value for a comparison that has been published since
    Beta 11.9, and never falls back across languages.
    """
    snapshot = comparison.live_entries
    if snapshot is None:
        return _entries_from_live_rows(comparison, language_code)
    if not isinstance(snapshot, list):
        # Defensive: a malformed snapshot is treated as "published with no
        # entries" rather than silently falling back to the draft rows.
        return []
    return _entries_from_snapshot(snapshot, language_code)


def _snapshot_tool_ids(snapshot: list) -> set[int]:
    return {
        item.get("tool_id")
        for item in snapshot
        if isinstance(item, dict) and item.get("tool_id") is not None
    }


def live_tool_ids_for_comparisons(comparisons_qs) -> dict[int, set[int]]:
    """
    Beta 11.9C: bulk equivalent of ``public_tool_entries()``'s tool-ID half,
    for surfaces that need every visible comparison's published tool
    membership at once (the comparison list's category filter) rather than
    one comparison's full entry content.

    Honours the identical State-A/State-C boundary:

    * State A (``live_entries`` is a list) - tool IDs come only from the
      snapshot, exactly like ``_entries_from_snapshot()``.
    * State C (``live_entries is None``) - only for comparisons that are, as
      filtered into ``comparisons_qs``, strictly ``published`` (the only
      status ``visible_on_site()`` lets a NULL-snapshot row through under -
      see its docstring). Their live rows stand in via one bulk
      ``ComparisonToolEntry`` query, never one query per comparison.

    ``comparisons_qs`` should already be the caller's visible queryset (e.g.
    ``Comparison.objects.visible_in_language(lang)``); this function adds
    exactly one query for the comparison rows themselves, plus at most one
    further query for any State-C legacy comparisons among them - never a
    query per comparison, per entry, or per tool. It performs no tool-
    visibility resolution and no category lookup; callers combine the
    returned tool IDs with ``Tool.objects.public()`` themselves.
    """
    tool_ids_by_comparison: dict[int, set[int]] = {}
    legacy_pks: list[int] = []

    for pk, snapshot, status in comparisons_qs.values_list("pk", "live_entries", "status"):
        if isinstance(snapshot, list):
            tool_ids_by_comparison[pk] = _snapshot_tool_ids(snapshot)
        elif snapshot is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
            tool_ids_by_comparison[pk] = set()
            legacy_pks.append(pk)
        else:
            # Fail-closed: any other combination (malformed snapshot, or a
            # NULL snapshot outside the documented legacy status - which
            # visible_on_site() should never let through) contributes
            # nothing rather than guessing.
            tool_ids_by_comparison[pk] = set()

    if legacy_pks:
        legacy_rows = ComparisonToolEntry.objects.filter(
            comparison_id__in=legacy_pks
        ).values_list("comparison_id", "tool_id")
        for comparison_id, tool_id in legacy_rows:
            tool_ids_by_comparison[comparison_id].add(tool_id)

    return tool_ids_by_comparison


def public_tools_for_comparisons(comparisons) -> dict[int, list[Tool]]:
    """
    Beta 11.9D: comparison.pk -> ordered list of public Tool objects, for
    surfaces that render tool badges for several already-loaded comparisons
    at once (the list page's cards) rather than one comparison's full entry
    content (``public_tool_entries()``) or a set of bare IDs
    (``live_tool_ids_for_comparisons()``, which cannot preserve order - a
    Python ``set``).

    ``comparisons`` is any iterable of already-fetched ``Comparison``
    instances (e.g. one paginated page) with ``live_entries``/``status``
    already loaded - unlike ``live_tool_ids_for_comparisons()``, this
    function never re-queries the Comparison table itself, so callers
    should pass exactly the objects they are about to render, not a fresh
    queryset.

    Honours the identical State-A/State-C boundary as every other public
    Comparison-entry surface:

    * State A (``live_entries`` is a list) - tool IDs and their order come
      only from the snapshot.
    * State C (``live_entries is None``, and - as ``visible_on_site()``
      already guarantees for any comparison reaching a public surface -
      strictly ``published``) - the live rows stand in, ordered exactly as
      ``_entries_from_live_rows()`` orders them, via one bulk
      ``ComparisonToolEntry`` query for every State-C comparison in the
      input combined.

    Tools are then resolved with a single ``Tool.objects.public().in_bulk()``
    call across every comparison's IDs combined; a tool that is missing,
    not yet public or withdrawn is dropped from its comparison's list
    without disturbing the relative order of the remaining tools. Total
    query count is at most two - one for State-C legacy rows (only if any
    are present) and one for the tools - regardless of how many comparisons
    or tools are involved.
    """
    comparisons = list(comparisons)
    ordered_tool_ids_by_pk: dict[int, list[int]] = {}
    legacy_pks: list[int] = []

    for comparison in comparisons:
        snapshot = comparison.live_entries
        if isinstance(snapshot, list):
            ordered_tool_ids_by_pk[comparison.pk] = [
                item.get("tool_id")
                for item in snapshot
                if isinstance(item, dict) and item.get("tool_id") is not None
            ]
        elif snapshot is None and comparison.status == EditorialWorkflowMixin.STATUS_PUBLISHED:
            ordered_tool_ids_by_pk[comparison.pk] = []
            legacy_pks.append(comparison.pk)
        else:
            # Fail-closed: same defensive branch as
            # live_tool_ids_for_comparisons() - never guess a source.
            ordered_tool_ids_by_pk[comparison.pk] = []

    if legacy_pks:
        legacy_rows = (
            ComparisonToolEntry.objects.filter(comparison_id__in=legacy_pks)
            .order_by("comparison_id", "position", "pk")
            .values_list("comparison_id", "tool_id")
        )
        for comparison_id, tool_id in legacy_rows:
            ordered_tool_ids_by_pk[comparison_id].append(tool_id)

    all_tool_ids = {
        tool_id for ids in ordered_tool_ids_by_pk.values() for tool_id in ids
    }
    tools_by_id = Tool.objects.public().in_bulk(all_tool_ids) if all_tool_ids else {}

    return {
        comparison.pk: [
            tools_by_id[tool_id]
            for tool_id in ordered_tool_ids_by_pk.get(comparison.pk, [])
            if tool_id in tools_by_id
        ]
        for comparison in comparisons
    }
