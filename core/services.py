from __future__ import annotations

import difflib
import re
from html import escape
from typing import Any, Dict, List, Tuple, Optional, Iterable

from django.conf import settings
from django.core.cache import cache
from django.db.models import Case, Count, IntegerField, Q, QuerySet, Value, When
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import get_language, override
from parler.utils.context import switch_language
from reversion.models import Version

from catalog.models import Category, Tool
from compare.models import Comparison
from core.projections import public_content_url, public_content_value
from core.text import visible_text
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase


def get_latest_items(limit: int = 6, mix: Tuple[int, int, int, int] = (4, 3, 2, 1),
                      language_code: str | None = None) -> List[Dict[str, Any]]:
    """
    Returns a balanced, recency-sorted mix of Guides/Prompts/UseCases/
    Comparisons based on mix; includes robust fallbacks when a type has too
    few items.

    Language handling (Beta 8.9a): all four types now use the strict,
    explicit-language visible_in_language(lang) (no cross-language fallback).
    Guide/UseCase were confirmed via empirical testing (Beta 8.9a) to leak
    EN-only content onto the DE homepage (and vice versa) under their
    previous .published manager (ambient-language active_translations()
    fallback) - the generated link did not 404, it silently rendered the
    wrong language under the active prefix. GuideQuerySet/UseCaseQuerySet's
    visible_in_language() is used here only for this homepage-teaser leak;
    GuideListView/GuideDetailView keep their existing behavior unchanged
    (UseCaseListView/UseCaseDetailView were separately hardened for their
    own, related persona-crash fix - see related_usecases()).
    """
    lang = language_code or get_language()
    g_need, p_need, u_need, c_need = mix

    # Wrapped in override(lang): to_teaser_item()'s reverse() calls resolve
    # the i18n_patterns URL prefix from Django's *ambient* active language,
    # not from an explicit parameter - this guarantees the generated links
    # match `lang` even when called with a language_code that differs from
    # whatever happens to be ambient (harmless in real request handling,
    # where the caller's language_code is always derived from get_language()
    # in the first place, but required for this function to be reliably
    # testable/reusable with an explicit language_code).
    with override(lang):
        g_qs: QuerySet = _safe_order_by_published(Guide.objects.visible_in_language(lang))
        p_qs: QuerySet = _safe_order_by_published(Prompt.objects.visible_in_language(lang))
        u_qs: QuerySet = _safe_order_by_published(UseCase.objects.visible_in_language(lang))
        c_qs: QuerySet = _safe_order_by_published(Comparison.objects.visible_in_language(lang))

        items: List[Dict[str, Any]] = []

        g_pick = list(g_qs[:g_need])
        p_pick = list(p_qs[:p_need])
        u_pick = list(u_qs[:u_need])
        c_pick = list(c_qs[:c_need])

        items.extend([to_teaser_item(g, "guide") for g in g_pick])
        items.extend([to_teaser_item(p, "prompt") for p in p_pick])
        items.extend([to_teaser_item(u, "usecase") for u in u_pick])
        items.extend([to_teaser_item(c, "comparison") for c in c_pick])

        # Fill remaining slots with the most recent leftover items (all types together)
        deficit = max(0, limit - len(items))
        if deficit:
            taken_ids = {
                *[("guide", g.pk) for g in g_pick],
                *[("prompt", p.pk) for p in p_pick],
                *[("usecase", u.pk) for u in u_pick],
                *[("comparison", c.pk) for c in c_pick],
            }

            def rest(qs, kind):
                for obj in qs:
                    key = (kind, obj.pk)
                    if key not in taken_ids:
                        yield to_teaser_item(obj, kind)

            merged = []
            merged.extend(list(rest(g_qs[g_need: g_need + limit * 2], "guide")))
            merged.extend(list(rest(p_qs[p_need: p_need + limit * 2], "prompt")))
            merged.extend(list(rest(u_qs[u_need: u_need + limit * 2], "usecase")))
            merged.extend(list(rest(c_qs[c_need: c_need + limit * 2], "comparison")))
            merged.sort(key=lambda x: (x.get("date") or 0), reverse=True)
            items.extend(merged[:deficit])

    items.sort(key=lambda x: (x.get("date") or 0), reverse=True)
    return items[:limit]


def related_guides(guide, limit=6, language_code: str | None = None):
    """
    Finds relevant Guides by shared categories and tools;
    falls back to temporally close Guides when metadata is sparse;
    excludes the current item.

    Beta 8.10: uses visible_in_language(lang) (strict, explicit language, no
    cross-language fallback) instead of the .published manager's ambient,
    fallback-inclusive active_translations() - empirically confirmed an
    EN-only guide could rank as the top "related" result under a German
    guide's page. Status rule widens from the .published manager's strict
    published() to visible_on_site() (matching GuideListView/GuideDetailView),
    so a guide with a live revision recommends from - and can be recommended
    from - the same publicly-visible pool as the page it's shown on. Ranking
    weights, order, and limit are unchanged.
    """

    if guide is None:
        return Guide.objects.none()
    lang = language_code or get_language()
    cat_ids = _ids(guide.categories.all()) if hasattr(guide, "categories") else []
    tool_ids = (
        _ids(guide.tools.all()) if hasattr(guide, "tools") else []
    )

    qs = Guide.objects.visible_in_language(lang).exclude(pk=guide.pk).prefetch_related(
        "categories", "tools__translations"
    )

    qs = (
        qs.filter(Q(categories__in=cat_ids) | Q(tools__in=tool_ids))
        .annotate(
            cat_matches=Count(
                "categories", filter=Q(categories__in=cat_ids), distinct=True
            ),
            tool_matches=Count(
                "tools", filter=Q(tools__in=tool_ids), distinct=True
            ),
        )
        .annotate(score=Count("id"))
        .order_by("-cat_matches", "-tool_matches", "-published_at")
        .distinct()
    )

    items = list(qs[:limit])
    if len(items) < limit:
        fallback = Guide.objects.visible_in_language(lang).exclude(
            pk__in=[g.pk for g in items] + [guide.pk]
        ).order_by("-published_at")[: limit - len(items)]
        items.extend(fallback)
    return items[:limit]


def related_prompts(prompt, limit=6, language_code: str | None = None):
    """
    Like related_guides but for Prompts: ranks by overlapping categories/tools;
    uses a time-based fallback if relations are missing.

    Beta 8.9: uses visible_in_language() (strict, explicit language, no
    cross-language fallback) instead of the .published manager, so "Related
    Prompts" on a detail page can never link to a translation-less prompt
    that would 404 under the active language (see Beta 8.8's strict
    PromptDetailView resolution).
    """
    lang = language_code or get_language()
    tag_ids = _ids(prompt.tags.all()) if hasattr(prompt, "tags") else []
    tool_ids = _ids(prompt.tools.all()) if hasattr(prompt, "tools") else []

    qs = Prompt.objects.visible_in_language(lang).exclude(pk=prompt.pk).prefetch_related("tags", "tools__translations")

    if tag_ids or tool_ids:
        qs = (
            qs.filter(Q(tags__in=tag_ids) | Q(tools__in=tool_ids))
            .annotate(
                tag_matches=Count("tags", filter=Q(tags__in=tag_ids), distinct=True),
                tool_matches=Count(
                    "tools", filter=Q(tools__in=tool_ids), distinct=True
                ),
            )
            .order_by("-tag_matches", "-tool_matches", "-published_at")
        )
    else:
        qs = qs.order_by("-published_at")

    items = list(qs[:limit])
    if len(items) < limit:
        fallback = Prompt.objects.visible_in_language(lang).exclude(
            pk__in=[p.pk for p in items] + [prompt.pk]
        ).order_by("-published_at")[: limit - len(items)]
        items.extend(fallback)
    return items[:limit]


def _live_usecase_persona(usecase, language_code: str) -> str:
    """
    The published persona for `language_code`, or "" when there is no live
    signal to rank on - never the current draft translation.

    Deliberately narrower than :func:`core.projections.public_content_value`:
    that helper's state-C fallback (an entirely empty ``live_i18n``, i.e. a
    record predating the snapshot mechanism) reads the current translation
    instead, which is the right legacy contract for rendered fields. Persona
    ranking must not do that - since Beta 11.7A every currently-published use
    case has a real snapshot (backfilled by
    usecases/migrations/0006_backfill_usecase_live_state.py), so a missing
    snapshot or a missing "persona" key both mean exactly the same thing: no
    persona signal for this candidate, not "check the draft instead".
    """
    snapshot = (getattr(usecase, "live_i18n", None) or {}).get(language_code)
    if not isinstance(snapshot, dict):
        return ""
    return snapshot.get("persona") or ""


def related_usecases(usecase, limit=6, language_code: str | None = None):
    """
    Like above for UseCases;
    ensures “something useful” is returned even for fresh entries with little metadata.

    Beta 8.9a: fixes a pre-existing FieldError - `persona` lives on the
    UseCaseTranslation model, not on UseCase itself, so it must be looked up
    via `translations__persona` (guarded by translations__language_code=lang
    so a candidate is only persona-matched via its OWN active-language
    translation row, never a different language's translation that happens
    to share the same text). Also switches the base visibility queryset from
    the .published manager (ambient-language, fallback-inclusive) to
    visible_in_language(lang) (strict, explicit language, no cross-language
    fallback), so a related use case can never render the wrong language
    under the active prefix. Ranking weights/order and the persona-vs-tool
    priority are unchanged; only the field path, base visibility, language
    filter and duplicate-avoidance were touched.

    Beta 8.13: the persona_match ranking annotation used a case-SENSITIVE
    comparison (translations__persona=persona) while the candidate filter
    above it already used a case-insensitive one (translations__persona__iexact
    =persona) - a candidate whose persona differed from the current use
    case's only in case (e.g. "founder" vs "Founder") passed the filter
    (so it was included in the related pool) but scored persona_match=0 in
    the ranking, the same as a candidate with a genuinely different
    persona, instead of tying with an exact-case match. The annotation now
    uses the same __iexact lookup, same translations__language_code=lang
    guard, as the filter above it (no substring/icontains semantics), so
    the filter and the ranking can never disagree on case again. Weights,
    order_by field order, limit and fallback-fill are unchanged.

    Beta 8.13a: two follow-ups confirmed via reproduction.

    1. The source persona used to be read via
       usecase.safe_translation_getter("persona", any_language=False) with
       no explicit language_code - that reads whichever language happens
       to be the object instance's OWN current parler language
       (usecase.get_current_language()), not necessarily `lang`. A use
       case instance whose current language is "en" but ranked here with
       language_code="de" would silently rank against its EN persona text
       while every candidate is matched against its own DE translation -
       so a genuinely matching DE candidate never scored a point. Fixed by
       reading the persona from `lang` explicitly, guarded by
       has_translation(lang): safe_translation_getter(language_code=lang)
       alone is not enough, because parler's own internal fallback lookup
       (triggered whenever language_code differs from the instance's
       current language) always passes use_fallback=True regardless of
       any_language - it would silently substitute PARLER_LANGUAGES'
       fallback language's persona text for a missing `lang` translation.
       has_translation(lang) first guarantees persona is only ever read
       from `lang` itself, with no cross-language substitution; a missing
       translation in `lang` now correctly yields persona="". Neither
       usecase.get_current_language() nor Django's active language is
       touched by this lookup (has_translation() and
       safe_translation_getter(language_code=...) both read via the
       instance's translation cache/DB without calling
       set_current_language()/translation.activate()).

    2. persona_match could be 1 for two use cases that both simply have no
       persona set at all, whenever they also shared a tool (the
       persona_q used for that candidate was unconditionally built as
       Q(translations__persona__iexact="") in that case, matching any
       other candidate with an equally empty persona). An empty/missing
       source persona now short-circuits persona_match to a constant
       Value(0) - no query is added, no candidate can score a persona
       point from two absent personas. Tool-based candidate selection and
       tool_matches ranking are unaffected either way.

    Beta 11.7B: both the source and the candidate side of persona matching
    were still reading the current *draft* translation
    (``translations__persona``, joined via the live translation table),
    which the Beta 11.7 visibility widening turned into a real draft leak -
    not of rendered text, but of *behaviour*: editing a published use case's
    persona and sending it to review/rework changed which candidates it
    matched (and which candidates it appeared as a match for) before that
    edit was ever published. Both sides now read
    :func:`_live_usecase_persona`, i.e. ``live_i18n[lang]["persona"]``, via
    a JSONField key-transform lookup (``live_i18n__<lang>__persona__iexact``)
    - so the candidate match is evaluated in the same query, with no
    additional query per candidate, exactly like the join it replaces. A
    candidate with no live persona for `lang` (missing snapshot, missing
    key, or empty value) simply cannot match - the lookup resolves to SQL
    NULL, which no ``__iexact`` comparison satisfies. Everything else -
    weights, the persona-vs-tool priority, order_by, limit, fallback-fill -
    is unchanged.
    """
    lang = language_code or get_language() or "en"
    persona = _live_usecase_persona(usecase, lang)
    tool_ids = _ids(usecase.tools.all()) if hasattr(usecase, "tools") else []

    qs = UseCase.objects.visible_in_language(lang).exclude(pk=usecase.pk).prefetch_related("tools")

    persona_q = Q()
    if persona:
        persona_q = Q(**{f"live_i18n__{lang}__persona__iexact": persona})

    if persona or tool_ids:
        if persona:
            persona_match_annotation = Case(
                When(persona_q, then=Value(1)), default=Value(0), output_field=IntegerField()
            )
        else:
            persona_match_annotation = Value(0, output_field=IntegerField())
        qs = (
            qs.filter(persona_q | Q(tools__in=tool_ids))
            .annotate(
                persona_match=persona_match_annotation,
                tool_matches=Count("tools", filter=Q(tools__in=tool_ids), distinct=True),
            )
            .order_by("-persona_match", "-tool_matches", "-published_at")
            .distinct()
        )
    else:
        qs = qs.order_by("-published_at")

    items = list(qs[:limit])
    if len(items) < limit:
        fallback = UseCase.objects.visible_in_language(lang).exclude(
            pk__in=[u.pk for u in items] + [usecase.pk]
        ).order_by("-published_at")[: limit - len(items)]
        items.extend(fallback)
    return items[:limit]


def related_comparisons(comparison: Comparison, limit: int = 6, language_code: str | None = None) -> List[Comparison]:
    """
    Finds related Comparisons by overlapping tools (and tool categories as a secondary signal).
    Returns Comparison instances; callers convert via to_teaser_item(..., "comparison"),
    matching related_guides()/related_prompts()/related_usecases().

    Beta 8.11a: uses visible_in_language(lang) (strict, explicit language, no
    cross-language fallback) instead of the .published manager - Comparison.
    published's own active_translations(get_language()) call does not
    actually restrict the queryset to objects with a genuine translation in
    the active language (PARLER_LANGUAGES' hide_untranslated=False lets
    fallback-translated objects through), so an EN-only comparison could be
    selected as a "related" card on a German comparison's detail page.
    Beta 8.11's snapshot-safe teaser resolver (_public_teaser_value(),
    Comparison.get_absolute_url()) correctly refuses to substitute another
    language's title/slug for such an object, but that only means the
    resulting card had title="" and url="#" - it was never excluded from
    the list itself. Confirmed via reproduction in
    compare/tests/test_related_comparisons_language_safety.py. Ranking
    weights, order, and limit are unchanged; only the base visibility/
    language filter (here and in the fallback-fill query) was touched.
    """

    if comparison is None:
        return []
    lang = language_code or get_language()

    tool_ids = _ids(comparison.tools.all()) if hasattr(comparison, "tools") else []

    cat_ids = list(
        comparison.tools.values_list("categories__id", flat=True).distinct()
    ) if tool_ids else []

    qs = (
        Comparison.objects.visible_in_language(lang)
        .exclude(pk=comparison.pk)
        .prefetch_related("tools", "tools__categories")
    )

    if tool_ids or cat_ids:
        qs = (
            qs.filter(Q(tools__in=tool_ids) | Q(tools__categories__in=cat_ids))
            .annotate(
                tool_matches=Count("tools", filter=Q(tools__in=tool_ids), distinct=True),
                cat_matches=Count(
                    "tools__categories", filter=Q(tools__categories__in=cat_ids), distinct=True
                ),
            )
            .order_by("-tool_matches", "-cat_matches", "-published_at")
        )
    else:
        qs = qs.order_by("-published_at")

    items = list(qs[:limit])

    # fallback: always return something useful
    if len(items) < limit:
        fallback = Comparison.objects.visible_in_language(lang).exclude(
            pk__in=[c.pk for c in items] + [comparison.pk]
        ).order_by("-published_at")[: limit - len(items)]
        items.extend(fallback)

    return items[:limit]


# ---------- Public inventory (Beta 8.7) ----------

PUBLIC_INVENTORY_CACHE_VERSION = "v5"  # v1 -> v2 (Beta 8.8): prompts_count is
# now language-strict (visible_in_language), so any v1 cache entry holds a
# semantically wrong (language-independent) prompt count and must never be
# served again after deployment - bumping the version key achieves that
# without a blanket cache.clear(). v2 -> v3 (Beta 8.9): comparisons_count is
# now language-strict (visible_in_language) too, for the same reason.
# v3 -> v4 (Beta 8.9a): usecases_count is now language-strict
# (visible_in_language) too, matching UseCaseListView's hardened query.
# v4 -> v5 (Beta 8.10): guides_count is now language-strict
# (visible_in_language) too, matching GuideListView's hardened query.
PUBLIC_INVENTORY_CACHE_TIMEOUT = 300  # seconds; see docstring on get_public_inventory
PUBLIC_INVENTORY_TOP_CATEGORIES_LIMIT = 6


def _normalize_language_code(language_code: str | None) -> str:
    """
    Resolves a supported project language code: the given code if valid,
    otherwise the current Django language, otherwise LANGUAGE_CODE.
    """
    supported = {code for code, _label in settings.LANGUAGES}
    candidate = (language_code or get_language() or settings.LANGUAGE_CODE or "en")[:2]
    return candidate if candidate in supported else settings.LANGUAGE_CODE


def resolve_public_starter_guide(language_code: str) -> Dict[str, str] | None:
    """
    Resolves the public starter guide (is_starter=True, published, with an
    active translation in language_code) as primitive data - no historical
    slug convention involved. Returns None if there is no published starter
    or it has no translation in language_code (a fallback-language
    translation must never be linked as if it were language_code).
    """
    guide = (
        Guide.objects.published()
        .filter(is_starter=True)
        .order_by("-published_at", "-pk")
        .first()
    )
    if guide is None:
        return None
    if not guide.has_translation(language_code):
        return None

    return {
        "title": guide.safe_translation_getter("title", language_code=language_code) or "",
        "url": guide.get_absolute_url(language=language_code),
    }


def _visible_tool_count_filter(now) -> Q:
    """
    Per-category "how many public tools" filter.

    Mirrors ToolQuerySet.public() (published_at <= now) but has to be
    expressed as a Q across the reverse `tools` relation, so it cannot
    literally reuse that queryset method. Verified by
    catalog/tests/test_public_tool_visibility.py, which asserts both sides
    agree - a category whose only tools are scheduled for the future must
    count 0 and must not surface as a well-stocked category.
    """
    return Q(tools__published_at__lte=now)


def _compute_public_inventory(lang: str) -> Dict[str, Any]:
    """
    Builds the fully-evaluated, cache-ready public inventory for one language.
    Reuses the same public visibility rules as the corresponding public list
    views instead of re-deriving status/language logic here.
    """
    with override(lang):
        now = timezone.now()

        # Beta 8.14a: unchanged semantics, now expressed via the central
        # ToolQuerySet.public() - so the count can never drift from what
        # the catalog/detail/sitemap surfaces consider public.
        tools_count = Tool.objects.public(at=now).count()

        categories_qs = (
            Category.objects.translated(lang)
            .annotate(visible_tool_count=Count("tools", filter=_visible_tool_count_filter(now), distinct=True))
            .filter(visible_tool_count__gt=0)
            .order_by("-visible_tool_count", "translations__name", "pk")
        )
        categories_count = categories_qs.count()

        catalog_list_url = reverse("catalog:list")
        top_categories = [
            {
                "name": cat.safe_translation_getter("name", language_code=lang) or "",
                "slug": cat.safe_translation_getter("slug", language_code=lang) or "",
                "tool_count": cat.visible_tool_count,
                "url": f"{catalog_list_url}?category={cat.safe_translation_getter('slug', language_code=lang)}",
            }
            for cat in categories_qs[:PUBLIC_INVENTORY_TOP_CATEGORIES_LIMIT]
        ]

        # Guides (Beta 8.10): same strict query GuideListView now uses -
        # every counted guide's detail URL is reachable in lang.
        guides_count = Guide.objects.visible_in_language(lang).count()
        # Prompts (Beta 8.8): same query PromptListView now uses - strict,
        # no cross-language fallback (unlike Guide's active_translations).
        prompts_count = Prompt.objects.visible_in_language(lang).count()
        # UseCases (Beta 8.9a): same strict query UseCaseListView now uses -
        # every counted use case's detail URL is reachable in lang.
        usecases_count = UseCase.objects.visible_in_language(lang).count()
        # Comparisons (Beta 8.9): same strict query ComparisonListView now
        # uses - every counted comparison's detail URL is reachable in lang.
        comparisons_count = Comparison.objects.visible_in_language(lang).count()

        starter_guide = resolve_public_starter_guide(lang)

    return {
        "counts": {
            "tools": tools_count,
            "categories": categories_count,
            "guides": guides_count,
            "prompts": prompts_count,
            "usecases": usecases_count,
            "comparisons": comparisons_count,
        },
        "top_categories": top_categories,
        "starter_guide": starter_guide,
    }


def get_public_inventory(language_code: str | None = None) -> Dict[str, Any]:
    """
    Returns cached, language-isolated public inventory counts and highlights
    (tool/category/guide/prompt/usecase/comparison counts, up to 6 well-
    stocked categories, the public starter guide) for the homepage and
    global footer.

    Cache: language-specific key, PUBLIC_INVENTORY_CACHE_TIMEOUT (300s)
    timeout. Editorial changes become visible after at most that many
    seconds (eventual consistency) rather than through signal-based
    invalidation. The returned dict contains only primitive values - no
    QuerySets or model instances - and is safe to store in any cache
    backend.
    """
    lang = _normalize_language_code(language_code)
    cache_key = f"mentoroai:public-inventory:{PUBLIC_INVENTORY_CACHE_VERSION}:{lang}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    data = _compute_public_inventory(lang)
    cache.set(cache_key, data, timeout=PUBLIC_INVENTORY_CACHE_TIMEOUT)
    return data


# ---------- Helpers ----------


def _safe_order_by_published(qs):
    try:
        str(qs.order_by("-published_at").query)
        return qs.order_by("-published_at")
    except Exception:
        return qs.order_by("-id")


def _first(seq):
    try:
        return next(iter(seq)) if seq else ""
    except Exception:
        return ""


def _public_teaser_value(obj, field: str, language_code: str | None = None) -> str:
    """
    Beta 8.10a: returns the public, live-revision-safe value for a
    translated field ("title", "intro", "body", ...) - never the raw,
    possibly draft-in-progress translation.

    Beta 10.4: the rule itself now lives in core.projections, so the search
    adapters can reuse it (and its database-expression twin) without
    importing a private helper. This wrapper only resolves the ambient
    language for the existing teaser call sites, which may omit it; the
    public API requires the language explicitly. An unresolvable language
    keeps returning "" rather than raising, matching prior behavior.
    """
    lang = language_code or get_language()
    if not lang:
        return ""
    return public_content_value(obj, field, language_code=lang)


def _public_teaser_url(obj, kind: str, language_code: str | None = None) -> str:
    """
    Beta 8.10a: returns a live-revision-safe public URL for a teaser card.

    Guide/Prompt/UseCase's own get_absolute_url() already checks the
    live_i18n snapshot before the current translation - delegating to it
    directly (instead of reversing obj.slug, the raw current-translation
    value) closes the same draft-slug leak fixed in
    guides/views.py::_resolve_guide_by_slug().

    Beta 8.11: Comparison.get_absolute_url() now does the same (live_i18n
    first, has_translation()-guarded translation fallback, "#" instead of
    raising for a missing translation) - the former comparison-specific
    branch here (which duplicated that same logic at this one call site
    because the model method didn't do it yet) is gone; every kind now
    goes through the same, single code path.

    Beta 10.4: delegates to core.projections.public_content_url, shared with
    the search adapters.
    """
    lang = language_code or get_language()
    if not lang:
        return obj.get_absolute_url()
    return public_content_url(obj, language_code=lang)


def teaser_for_guide(g: Guide, language_code: str | None = None) -> str:
    """
    Returns the full visible-text intro/body for a Guide, for the editorial
    card to shorten.

    Beta 10.9 correction: this used to pre-cut the text itself with
    ``strip_tags(src)[:160]`` - a blind slice with no word-boundary check and
    no "..." marker, and ``strip_tags`` glues adjacent block elements
    together ("moechten.Erfahre") instead of leaving a space. Both defects
    then reached the browser untouched, because the card's own `summarize`
    filter only shortens text that is still over its length limit - text
    already cut to 160 characters never was. Returning the plain, complete
    text here and letting the card be the single place that shortens it
    (core.text.summarize_html, via the `summarize` template filter) is what
    Beta 10.9 already does for every other editorial card; this closes the
    one remaining path that duplicated the cut instead of sharing it.
    """
    src = (
        _public_teaser_value(g, "intro", language_code)
        or _public_teaser_value(g, "body", language_code)
    )
    return visible_text(src)


def teaser_for_prompt(p: Prompt, language_code: str | None = None) -> str:
    """Teaser builder for a Prompt - see teaser_for_guide for the rationale."""
    ex = ""
    examples = getattr(p, "examples", None)
    if isinstance(examples, (list, tuple)):
        ex = _first(examples)
    elif isinstance(examples, str):
        ex = examples

    body = (
        _public_teaser_value(p, "intro", language_code)
        or _public_teaser_value(p, "body", language_code)
    )

    src = ex or body
    return visible_text(src)


def teaser_for_usecase(u: UseCase, language_code: str | None = None) -> str:
    """Teaser builder for a UseCase - see teaser_for_guide for the rationale."""
    src = _public_teaser_value(u, "intro", language_code)
    return visible_text(src)


def teaser_for_comparison(c: Comparison, language_code: str | None = None) -> str:
    """Teaser builder for a Comparison - see teaser_for_guide for the rationale."""
    src = (
        _public_teaser_value(c, "intro", language_code)
        or _public_teaser_value(c, "body", language_code)
    )
    return visible_text(src)


def to_teaser_item(obj, kind: str, language_code: str | None = None) -> Dict[str, Any]:
    """
    Factory that converts any supported object into the unified teaser dict
    based on kind to simplify front-end consumption.

    Beta 8.10a: title/teaser/url all go through the live-revision-safe
    _public_teaser_value()/_public_teaser_url() helpers - a guide (or
    prompt/use case/comparison) mid-revision must only ever surface its
    last published live title, intro/body and URL here, never the current
    draft translation or a draft slug.
    """
    if kind == "guide":
        return {
            "title": _public_teaser_value(obj, "title", language_code),
            "teaser": teaser_for_guide(obj, language_code=language_code),
            "url": _public_teaser_url(obj, kind, language_code),
            "date": getattr(obj, "updated_at", None),
            "badge": "Guide",
        }
    if kind == "prompt":
        return {
            "title": _public_teaser_value(obj, "title", language_code),
            "teaser": teaser_for_prompt(obj, language_code=language_code),
            "url": _public_teaser_url(obj, kind, language_code),
            "date": getattr(obj, "updated_at", None),
            "badge": "Prompt",
        }
    if kind == "usecase":
        return {
            "title": _public_teaser_value(obj, "title", language_code),
            "teaser": teaser_for_usecase(obj, language_code=language_code),
            "url": _public_teaser_url(obj, kind, language_code),
            "date": getattr(obj, "updated_at", None),
            "badge": "Usecase",
        }
    if kind == "comparison":
        return {
            "title": _public_teaser_value(obj, "title", language_code),
            "teaser": teaser_for_comparison(obj, language_code=language_code),
            "url": _public_teaser_url(obj, kind, language_code),
            "date": getattr(obj, "updated_at", None),
            "badge": "Comparison",
        }
    return {
        "title": str(obj),
        "teaser": "",
        "url": "#",
        "date": None,
        "badge": kind.title(),
    }


def _ids(qs, field="id"):
    return list(qs.values_list(field, flat=True))


TEXT_FIELDS = ("title", "intro", "body")
LIVE_FIELDS = ("slug", "public_slug", "title", "intro", "body")


def get_live_version_instance(obj):
    """
    Fetches the latest reversion Version marked as “live” to compare against the working copy;
    returns None if no live version exists.
    """
    rev_id = getattr(obj, "last_published_revision_id", None)
    if not rev_id:
        return None
    try:
        v = Version.objects.get(id=rev_id)
        live = v._object_version.object
        if hasattr(live, "set_current_language") and hasattr(obj, "get_current_language"):
            live.set_current_language(obj.get_current_language())
        return live
    except Version.DoesNotExist:
        return None


class _LiveSnapshotProxy:
    """
    Lightweight proxy that exposes translated “live” values (from live_i18n[lang])
    of a content object with safe fallbacks;
    used to render historical/live variants without mutating the real instance.
    """

    def __init__(self, obj, lang: str):
        self._obj = obj
        self._lang = lang
        self._data = (obj.live_i18n or {}).get(lang, {}) if hasattr(obj, "live_i18n") else {}

    def __getattr__(self, name):
        if name in LIVE_FIELDS:
            val = self._data.get(name)
            if val:
                return val
            with switch_language(self._obj, self._lang):
                return getattr(self._obj, "safe_translation_getter")(name)
        return getattr(self._obj, name)

    @property
    def title(self):
        return self.__getattr__("title")

    @property
    def intro(self):
        return self.__getattr__("intro")

    @property
    def body(self):
        return self.__getattr__("body")

    @property
    def slug(self):
        return self.__getattr__("slug")

    @property
    def public_slug(self):
        return self.__getattr__("public_slug")


def get_live_display_instance(obj, language: Optional[str] = None):
    """
    Builds a display-only proxy for the live snapshot in the requested (or current) language;
    safe to pass into templates/serializers.
    """
    lang = language or get_language()

    if hasattr(obj, "live_i18n") and (obj.live_i18n or {}).get(lang):
        return _LiveSnapshotProxy(obj, lang)

    try:
        live = get_live_version_instance(obj)
        if live:
            return live
    except Exception:
        pass

    return obj


_DIFF_HTML_FIELDS = ("intro", "body")
_SECTION_FIELDS = ("title", "body")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    """
    Removes HTML tags and collapses whitespace to make diffs and teasers readable;
    prevents script/style leakage into comparisons.
    """
    return _TAG_RE.sub("", s or "")


def _inline_diff(a: str, b: str) -> tuple[str, str]:
    """
    Creates a compact inline word-level diff (using difflib) with HTML escapes;
    truncates long outputs for UI friendliness.
    """
    sm = difflib.SequenceMatcher(a=a or "", b=b or "")
    out_a, out_b = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            eq_a = escape((a or "")[i1:i2])
            eq_b = escape((b or "")[j1:j2])
            out_a.append(eq_a)
            out_b.append(eq_b)
        elif tag == "replace":
            out_a.append(f"<del class='diff-ins'>{escape((a or '')[i1:i2])}</del>")
            out_b.append(f"<ins class='diff-del'>{escape((b or '')[j1:j2])}</ins>")
        elif tag == "delete":
            out_b.append(f"<del class='diff-del'>{escape((a or '')[i1:i2])}</del>")
        elif tag == "insert":
            out_a.append(f"<ins class='diff-ins'>{escape((b or '')[j1:j2])}</ins>")
    return ("".join(out_a), "".join(out_b))


def build_field_diffs(left: dict, right: dict) -> list[dict]:
    """
    Generates human-readable diffs for selected scalar/text fields between the working copy and the live snapshot;
    returns a dict keyed by field name.
    """
    diffs = []
    keys = {"slug", "public_slug", "title", "intro", "body"}
    for field in keys:
        lv = left.get(field) or ""
        rv = right.get(field) or ""
        if lv == rv:
            continue

        if field in _DIFF_HTML_FIELDS:
            la, rb = _inline_diff(_strip_html(lv), _strip_html(rv))
        else:
            la, rb = _inline_diff(str(lv), str(rv))

        diffs.append({"field": field, "left": la, "right": rb})
    return diffs


def _section_current_values(section, lang: str) -> dict:
    """
    Extracts the “current” values of a Guide/Prompt/UseCase section (title/body/items) in the active language;
    used for structured diffs.
    """
    if switch_language:
        with switch_language(section, lang):
            get = getattr(section, "safe_translation_getter", lambda n: None)
            return {f: get(f) for f in _SECTION_FIELDS}
    # Fallback ohne parler
    get = getattr(section, "safe_translation_getter", lambda n: None)
    return {f: get(f) for f in _SECTION_FIELDS}


def _section_live_values(section, lang: str) -> dict:
    """
    Reads the “live” values for the same section via the snapshot proxy;
    mirrors _section_current_values for apples-to-apples comparison.
    """
    data = (getattr(section, "live_i18n", None) or {}).get(lang, {})
    return {f: data.get(f) for f in _SECTION_FIELDS}


def build_section_diffs(guide, lang: str) -> list[dict]:
    """
    Produces per-field diffs for a section (title/body/items) using _inline_diff;
    enables precise review in editorial UIs.
    """
    diffs: list[dict] = []

    # actual Sections (Draft) – keep order
    sections: Iterable = getattr(guide, "sections", None)
    if hasattr(sections, "all"):
        sections = sections.all()
    if not sections:
        return diffs

    def _label(sec) -> str:
        left = _section_current_values(sec, lang).get("title")
        if left:
            return left
        right = _section_live_values(sec, lang).get("title")
        if right:
            return right
        return f"Section #{getattr(sec, 'pk', '—')}"

    for sec in sections:
        left_vals = _section_current_values(sec, lang)
        right_vals = _section_live_values(sec, lang)
        has_live = any(bool(v) for v in right_vals.values())
        has_draft = any(bool(v) for v in left_vals.values())
        if not has_live and not has_draft:
            continue
        if has_draft and not has_live:
            fields = []
            for f in _SECTION_FIELDS:
                lv = left_vals.get(f) or ""
                la, rb = _inline_diff(_strip_html(lv), "")
                fields.append({"field": f, "left": la, "right": rb})
            diffs.append({
                "id": getattr(sec, "pk", None),
                "label": _label(sec),
                "kind": "added",
                "fields": fields,
            })
            continue

        changed_fields: list[dict] = []
        for f in _SECTION_FIELDS:
            lv = left_vals.get(f) or ""
            rv = right_vals.get(f) or ""
            if lv == rv:
                continue
            la, rb = _inline_diff(_strip_html(lv), _strip_html(rv))
            changed_fields.append({"field": f, "left": la, "right": rb})

        if changed_fields:
            diffs.append({
                "id": getattr(sec, "pk", None),
                "label": _label(sec),
                "kind": "changed",
                "fields": changed_fields,
            })

    return diffs
