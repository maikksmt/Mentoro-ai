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

Draft preview (Beta 11.10)
---------------------------
``draft_tool_entries()`` is the deliberate mirror image of everything above:
it reads the currently *saved* ``ComparisonToolEntry`` rows for the admin's
private draft preview, never ``live_entries``, and does not filter tools
through ``Tool.objects.public()`` - see its own docstring. The rest of the
preview context (parent title/intro/body from the saved translation,
related comparisons, SEO) is assembled in ``compare/admin.py`` rather than
here: that needs ``core.services.related_comparisons()``/``to_teaser_item()``,
and ``core/services.py`` itself imports ``live_tool_ids_for_comparisons``
from this module, so importing ``core.services`` here would be circular.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse
from django.utils.translation import override as language_override

from catalog.models import Tool, ToolTranslation
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


@dataclass(frozen=True, slots=True)
class DraftToolView:
    """
    Beta 11.10B: one tool's already-materialized display data for the
    private draft preview - name, slug and a pre-built absolute URL.

    Deliberately not the lazy ``Tool`` model instance
    (:class:`DraftToolEntryView` used to hold one directly): ``Tool.name``
    is a Parler-translated field, and reading it - even after a
    ``prefetch_related("tool__translations")`` - makes django-parler consult
    its own per-instance translation cache, which this project backs with a
    ``DatabaseCache``. That is one additional SQL statement against
    ``mentoroai_cache_table`` (a *cache-table* read, but a real one) the
    first time any translated field is read on a given tool instance -
    confirmed to still happen even with the translations prefetched (Beta
    11.10B's own Variant-A trial: 18 queries, including cache table
    read+write pairs, for 3 tools). Scoped to *distinct tools referenced by
    this comparison's current draft entries* - never the whole catalogue -
    and resolved once, in bulk, by :func:`_resolve_draft_tools` before any
    of these objects exist. ``get_absolute_url()`` returns the pre-built
    string; it never touches the database.

    ``slug`` and ``absolute_url`` never needed a translation query at all:
    ``Tool.slug`` is a plain, untranslated field, and ``Tool
    .get_absolute_url()`` only ever reads ``self.slug`` (confirmed by
    reading ``catalog/models.py`` - it does not touch ``.name``), so both
    are simply read off the already-``select_related``-loaded base ``Tool``
    row.
    """

    pk: int
    name: str
    slug: str
    absolute_url: str

    def get_absolute_url(self) -> str:
        return self.absolute_url


def _draft_tool_fallback_languages() -> list[str]:
    """
    The real, configured ``PARLER_LANGUAGES`` fallback chain for the
    project's single language-group (``site_id`` isn't relevant here - see
    ``mentoroai/settings/base.py``), as a list regardless of whether the
    setting itself is a single code or a list. Read from the live setting
    rather than hardcoded, so a future change to ``PARLER_LANGUAGES``
    changes this resolver's behaviour too, exactly as it already changes
    ``Tool.name``'s own field-descriptor fallback.
    """
    fallback = settings.PARLER_LANGUAGES.get("default", {}).get(
        "fallback", settings.PARLER_DEFAULT_LANGUAGE_CODE
    )
    if isinstance(fallback, str):
        return [fallback]
    return list(fallback or [])


def _resolve_draft_tools(tools_by_id: dict, language_code: str) -> dict:
    """
    Bulk-materializes a :class:`DraftToolView` for every ``Tool`` instance
    in ``tools_by_id`` (already-fetched base rows, keyed by pk - typically
    ``{entry.tool_id: entry.tool for entry in entries}``), reproducing
    ``Tool.name``'s own per-field fallback chain exactly (preferred
    language, then ``PARLER_LANGUAGES``' configured fallback(s), in order)
    via exactly one bulk query against :class:`catalog.models.ToolTranslation`
    instead of one django-parler cache-table read per distinct tool.

    A tool with no translation in the preferred language *or* any
    configured fallback language yields ``name=""`` rather than raising -
    ``Tool.name``'s own field descriptor does raise ``Tool.DoesNotExist`` in
    that state, but that state cannot occur through normal admin use (every
    saved ``Tool`` has at least the language tab that was used to save it),
    and crashing the entire preview page over one such row would be worse
    for an authorized editor than an empty name on that one card - this is
    the one deliberate, narrow, explicitly-documented difference from the
    lazy field descriptor's behaviour, not a new translation semantics.
    """
    if not tools_by_id:
        return {}

    candidate_languages = [language_code] + [
        code for code in _draft_tool_fallback_languages() if code != language_code
    ]

    names_by_tool: dict[int, dict[str, str]] = {}
    for master_id, lang_code, name in ToolTranslation.objects.filter(
        master_id__in=tools_by_id.keys(), language_code__in=candidate_languages
    ).values_list("master_id", "language_code", "name"):
        names_by_tool.setdefault(master_id, {})[lang_code] = name

    views: dict[int, DraftToolView] = {}
    for tool_id, tool in tools_by_id.items():
        available = names_by_tool.get(tool_id, {})
        name = ""
        for lang_code in candidate_languages:
            if lang_code in available:
                name = available[lang_code]
                break
        with language_override(language_code):
            absolute_url = reverse("catalog:detail", kwargs={"slug": tool.slug})
        views[tool_id] = DraftToolView(
            pk=tool.pk, name=name, slug=tool.slug, absolute_url=absolute_url
        )
    return views


@dataclass(frozen=True)
class DraftToolEntryView:
    """
    One currently *saved* comparison entry, as the private admin draft
    preview renders it.

    Beta 11.10. Deliberately a separate type from :class:`PublicToolEntry`
    rather than a reuse: its ``tool`` may be any tool a
    ``ComparisonToolEntry`` row currently references, regardless of that
    tool's own ``published_at`` - an authorized draft preview must show the
    actually-selected tool even if it is not yet public, so a type literally
    named "Public..." would misstate what this object holds. Shape is
    otherwise identical to ``PublicToolEntry`` on purpose: the public detail
    template consumes both through the exact same attribute names
    (``tool``, ``label``, ``summary``, ``pros``, ``cons``, ``special``).

    ``tool`` holds a :class:`DraftToolView` (Beta 11.10B), not the lazy
    ``Tool`` model instance - see that class's docstring for why.
    """

    tool: DraftToolView
    label: str
    summary: str
    pros: str
    cons: str
    special: str


def draft_tool_entries(comparison, language_code: str) -> list[DraftToolEntryView]:
    """
    Beta 11.10: the comparison's currently *saved* tool entries, for the
    private admin draft preview - never ``Comparison.live_entries``.

    Reads exactly what has been saved to the database, never a published
    snapshot:

    * membership and order - every current ``ComparisonToolEntry`` row for
      ``comparison``, in ``(position, pk)`` order (mirrors
      ``ComparisonToolEntry.Meta.ordering`` and the same ordering
      ``build_live_entries()`` freezes at publish time), so a draft
      addition, tool swap, deletion or reorder is reflected immediately and
      a deleted row (cascade-deleted along with its tool, per the FK) is
      simply absent - there is no separate "restore" concept to fabricate;
    * the tool - the row's current ``tool_id``, resolved without any
      ``Tool.objects.public()`` filter. Unlike every public surface, a
      not-yet-public or scheduled tool is shown as-is: the preview is
      reached only through the authenticated, object-permission-checked
      admin route (see ``ComparisonAdmin.draft_preview_view``), so there is
      no public-visibility contract to enforce here, and hiding the
      author's actual tool selection would make the preview lie about what
      is saved;
    * entry text - the entry's own translation row for exactly
      ``language_code``. An entry without a saved translation in that
      language is omitted (fail-closed) rather than falling back to another
      language's text or ``any_language=True`` - mirroring
      ``_entries_from_snapshot()``'s per-entry fail-closed behaviour for a
      language missing from the live snapshot. This fail-closed contract is
      unrelated to and unchanged by the tool-name fallback in
      :func:`_resolve_draft_tools` below - entry text and tool display name
      are resolved independently, by design.

    Three queries total regardless of entry count (Beta 11.10B): one for
    the entry rows (with their tool joined via ``select_related``), one for
    every entry's translation rows via ``prefetch_related``, one bulk query
    resolving every *distinct* referenced tool's display name (see
    :func:`_resolve_draft_tools`) - never a query per entry, per tool, or a
    django-parler cache-table read per tool. No model is saved, mutated or
    reloaded.
    """
    entries = (
        comparison.tool_entries
        .select_related("tool")
        .prefetch_related("translations")
        .order_by("position", "pk")
    )
    entries = list(entries)

    tools_by_id = {entry.tool_id: entry.tool for entry in entries}
    draft_tools_by_id = _resolve_draft_tools(tools_by_id, language_code)

    views: list[DraftToolEntryView] = []
    for entry in entries:
        translation_row = next(
            (t for t in entry.translations.all() if t.language_code == language_code),
            None,
        )
        if translation_row is None:
            continue
        views.append(
            DraftToolEntryView(
                tool=draft_tools_by_id[entry.tool_id],
                **{field: (getattr(translation_row, field) or "") for field in ENTRY_LIVE_FIELDS},
            )
        )
    return views
