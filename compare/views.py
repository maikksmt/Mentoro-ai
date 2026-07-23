from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import Http404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import ListView, DetailView

from catalog.models import Category, Tool, ToolTranslation
from core.models.editorial import EditorialWorkflowMixin
from core.projections import public_content_value
from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.services import related_comparisons, to_teaser_item
from core.views import SeoMixin
from .models import Comparison, ComparisonTranslation
from .presentation import (
    live_tool_ids_for_comparisons,
    public_tool_entries,
    public_tools_for_comparisons,
)


def _resolve_by_slug(qs: QuerySet[Comparison], slug: str, language_code: str) -> Comparison | None:
    """
    Beta 8.11: mirrors guides/views.py::_resolve_guide_by_slug() and the
    identical Prompt/UseCase fixes - once a comparison has a live_i18n
    snapshot for language_code, that snapshot's slug is the SOLE public
    slug for that language; the current translation slug is not tried at
    all. Previously this resolver only ever matched the CURRENT
    translation's slug/public_slug with no live_i18n check whatsoever - so
    a translation slug diverging from its own live_i18n snapshot (while
    status stayed PUBLISHED) resolved the diverged slug instead of the live
    one. Confirmed via reproduction in
    compare/tests/test_url_language_safety.py.

    The narrow backward-compatibility fallback to the current translation's
    public_slug/slug applies ONLY to comparisons that are strictly
    `published` AND have no live_i18n entry for this language at all (a
    historical record predating the live-snapshot mechanism).

    Language matching is scoped throughout (translations__language_code=
    language_code), so a bilingual comparison's slug in the other language
    can never resolve under this prefix either.
    """
    live_match = (
        qs.filter(
            Q(**{f"live_i18n__{language_code}__public_slug": slug})
            | Q(**{f"live_i18n__{language_code}__slug": slug})
        )
        .distinct()
        .first()
    )
    if live_match:
        return live_match

    compat_qs = (
        qs.filter(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        .exclude(**{"live_i18n__has_key": language_code})
    )
    return (
        compat_qs.filter(
            Q(translations__language_code=language_code, translations__public_slug=slug)
            | Q(translations__language_code=language_code, translations__slug=slug)
        )
        .distinct()
        .first()
    )


def _filter_comparisons_by_query(
    queryset: QuerySet[Comparison],
    *,
    query: str,
    language_code: str,
) -> QuerySet[Comparison]:
    """
    Beta 10.2: restricts `queryset` to comparisons whose own translated text
    OR whose linked tools' names match `query` **in language_code only**.

    Previously this was expressed as extra lookups chained onto the already
    language-filtered queryset:

        qs.filter(
            Q(translations__title__icontains=q)
            | ... | Q(tools__translations__name__icontains=q)
        )

    Django opens a SEPARATE join for every filter() call spanning a
    multi-valued relation, so visible_in_language()'s language_code
    condition applied to the *visibility* join only, while the search join
    (and the whole tools -> tool_translation chain) stayed language-
    unbounded. The generated SQL contained exactly one language_code
    condition in total. A bilingual comparison whose search term occurred
    only in its English text therefore matched the German search and was
    then rendered with its German title - a title not containing the search
    term at all. The same applied to linked tool names, and symmetrically
    in the other direction. Confirmed by reproduction in
    compare/tests/test_search_language_safety.py.

    Using Exists() subqueries binds the language *inside* each subquery, so
    it cannot become detached from the text lookup by a later filter() call.
    It also keeps the outer query free of search joins entirely: the search
    can no longer multiply rows, and the resulting SQL has one language_code
    condition per searched relation instead of one in total.

    `language_code` is a required keyword argument on purpose - the search
    language must be passed explicitly rather than re-read from Django's
    ambient active language inside this helper.

    Deliberately unchanged: which fields are searched, the icontains
    semantics, the caller's visibility queryset, ordering and pagination.
    Tool rows are matched regardless of the tool's own published_at, exactly
    as before.
    """
    own_text_match = Exists(
        ComparisonTranslation.objects.filter(
            master_id=OuterRef("pk"),
            language_code=language_code,
        ).filter(
            Q(title__icontains=query)
            | Q(intro__icontains=query)
            | Q(body__icontains=query)
        )
    )
    tool_name_match = Exists(
        ToolTranslation.objects.filter(
            master__comparisons=OuterRef("pk"),
            language_code=language_code,
            name__icontains=query,
        )
    )
    return queryset.filter(own_text_match | tool_name_match)


def _resolve_category_pk(cat: str) -> int | None:
    """
    Resolves the `?category=` parameter to a Category pk.

    Byte-for-byte the pre-11.9C matching contract (a translated slug in any
    language, or the category's raw pk matched case-insensitively) - only
    *how the resolved pk is used* changes in this slice, not how a slug
    resolves to one. See ``ComparisonListView.get_queryset()`` for what
    changed: which comparisons/tools that pk is then checked against.
    """
    return (
        Category.objects.filter(Q(translations__slug=cat) | Q(pk__iexact=cat))
        .values_list("pk", flat=True)
        .distinct()
        .first()
    )


class ComparisonListView(SeoMixin, ListView):
    model = Comparison
    template_name = "compare/comparison_list.html"
    context_object_name = "objects"
    paginate_by = 15

    def _visible_queryset(self, lang):
        # Beta 8.9: visible_in_language() (strict, no cross-language
        # fallback) instead of the .published manager's active_translations()
        # fallback - every card's detail URL must actually resolve under
        # the active language (see ComparisonDetailView's strict slug match
        # from Beta 8.8).
        return Comparison.objects.visible_in_language(lang).distinct()

    def _live_tool_ids_by_pk(self, lang):
        """
        Cached per request (this view instance is per-request, like every
        Django CBV): both get_queryset()'s category filter and
        _categories_for_filters()'s dropdown need the identical mapping, and
        computing it once keeps the whole page at a small constant number of
        queries regardless of how many comparisons are visible.
        """
        if not hasattr(self, "_live_tool_ids_cache"):
            self._live_tool_ids_cache = live_tool_ids_for_comparisons(
                self._visible_queryset(lang)
            )
        return self._live_tool_ids_cache

    def get_queryset(self):
        lang = get_language()
        q = self.request.GET.get("q") or ""

        qs = self._visible_queryset(lang)

        # Beta 11.9C: the category filter previously joined the *current*
        # `tools` M2M (through ComparisonToolEntry, i.e. today's draft
        # rows) directly onto the visible queryset - so a draft tool swap,
        # a brand new draft entry, or a draft deletion could add or remove
        # a comparison from a category's results before republish, exactly
        # the class of leak Beta 11.9/11.9A/11.9B closed for the detail
        # page. It now uses only each comparison's published tool-ID
        # snapshot (state A) or the documented state-C legacy rows,
        # resolved through the same public Tool contract as the detail
        # page - see compare/presentation.py::live_tool_ids_for_comparisons().
        cat = self.request.GET.get("category") or self.request.GET.get("cat")
        if cat:
            category_pk = _resolve_category_pk(cat)
            if category_pk is None:
                qs = qs.none()
            else:
                public_tool_ids_in_category = set(
                    Tool.objects.public()
                    .filter(categories__pk=category_pk)
                    .values_list("pk", flat=True)
                )
                tool_ids_by_pk = self._live_tool_ids_by_pk(lang)
                matching_pks = [
                    pk
                    for pk, tool_ids in tool_ids_by_pk.items()
                    if tool_ids & public_tool_ids_in_category
                ]
                qs = qs.filter(pk__in=matching_pks)

        if q:
            # Beta 10.2: the search language is resolved once here and then
            # passed explicitly - see _filter_comparisons_by_query() for the
            # cross-language match leak this closes.
            qs = _filter_comparisons_by_query(qs, query=q, language_code=lang)

        return qs

    def _categories_for_filters(self):
        """
        Beta 11.9C: previously built from
        ``Category.objects.filter(tools__comparisons__in=ctx["object_list"])``
        - a double leak. ``tools__comparisons`` is the same current-draft M2M
        the category filter itself used to rely on, and ``object_list`` in a
        paginated ListView's context is only the *current page's* slice
        (see ``MultipleObjectMixin.get_context_data``), not the full visible
        set - so switching to page 2 could silently change which options
        page 1 had offered.

        Options now come from the complete language-visible comparison set
        (ignoring the request's own q/category params, so users can switch
        between filters rather than only narrow within the current result),
        via the same live tool-ID mapping and the same public Tool contract
        the filter itself now uses.
        """
        lang = get_language()
        tool_ids_by_pk = self._live_tool_ids_by_pk(lang)

        all_tool_ids: set[int] = set()
        for tool_ids in tool_ids_by_pk.values():
            all_tool_ids |= tool_ids

        if not all_tool_ids:
            return Category.objects.none()

        category_pks = set(
            Tool.objects.public()
            .filter(pk__in=all_tool_ids)
            .values_list("categories", flat=True)
        )
        category_pks.discard(None)
        if not category_pks:
            return Category.objects.none()

        return (
            Category.objects.translated(lang)
            .filter(pk__in=category_pks)
            .distinct()
            .order_by("translations__name")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        category_slug = self.request.GET.get("category") or ""
        q = self.request.GET.get("q") or ""

        title = _("AI tool comparisons")
        description = _(
            "Side-by-side comparisons of AI tools to help you understand strengths, weaknesses and special features."
        )

        canonical = absolute_url(reverse("compare:index"))
        alternates = localized_alternates(request, url_name="compare:index")

        ctx["seo"] = self.build_seo(
            request,
            title=title,
            description=seo_text(description),
            canonical=canonical,
            og_type="website",
            og_image=get_og_image(),
            alternates=alternates,
        )

        # Beta 11.9D: card tool badges, bundled once for exactly the
        # comparisons on this page - never the full filtered set (that
        # stays the category filter's own job, see
        # _categories_for_filters()), and never per-card. `display_tools`
        # is a transient presentation attribute, set here and never saved -
        # see compare/presentation.py::public_tools_for_comparisons() for
        # the State-A/State-C boundary and Tool.objects.public() contract
        # it shares with the detail page and the category filter.
        page_comparisons = list(ctx[self.context_object_name])
        tools_by_pk = public_tools_for_comparisons(page_comparisons)
        for comparison in page_comparisons:
            comparison.display_tools = tools_by_pk.get(comparison.pk, [])
        ctx[self.context_object_name] = page_comparisons

        ctx["categories"] = self._categories_for_filters()
        ctx["category"] = category_slug
        ctx["q"] = q
        ctx["crumbs"] = [
            (_("Comparisons"), request.path),
        ]
        return ctx


class ComparisonDetailView(SeoMixin, DetailView):
    """
    Detail view for a single comparison.
    """
    model = Comparison
    template_name = "compare/comparison_detail.html"
    context_object_name = "obj"

    def get_object(self, queryset=None):
        # Beta 8.11: visible_in_language(lang) (strict, explicit language,
        # no cross-language fallback) instead of Comparison.published.language(lang) -
        # PublishedOnlyManager's own active_translations(lang) call uses
        # hide_untranslated=False's fallback, so it does not actually
        # restrict the queryset to objects with a genuine `lang` translation;
        # .language(lang) chained after it only sets the iteration language,
        # it filters nothing. The explicit translations__language_code=lang
        # match below already prevented this from serving the wrong
        # language, but visible_in_language() makes that guarantee explicit
        # at the queryset level too, matching ComparisonListView.
        lang = get_language()
        slug = self.kwargs["slug"]

        qs = (
            Comparison.objects.visible_in_language(lang)
            .prefetch_related(
                "tools",
                "tool_entries",
                "tool_entries__tool",
            )
        )

        obj = _resolve_by_slug(qs, slug, lang)
        if not obj:
            raise Http404("Comparison not found.")
        return obj

    def _categories_for_object(self, obj: Comparison):
        """
        Collect all categories used by tools in this comparison.
        """
        cats = (
            Category.objects.filter(tools__comparisons=obj)
            .distinct()
            .order_by("translations__name")
        )
        return cats

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        obj: Comparison = ctx["obj"]
        lang = get_language()

        # Beta 11.9: the published values, via the canonical three-state
        # projection. The previous `get_live_value(...) or
        # safe_translation_getter(...)` chain fell through to the current
        # draft whenever a published value was legitimately empty.
        title = public_content_value(obj, "title", language_code=lang)
        intro = public_content_value(obj, "intro", language_code=lang)
        body = public_content_value(obj, "body", language_code=lang)

        description_source = intro or body
        description = seo_text(description_source or "")[:155]

        canonical = absolute_url(obj.get_absolute_url(language=lang))
        alternates = localized_alternates(request, obj=obj)
        # Beta 11.9: published entries only - see compare/presentation.py.
        entries = public_tool_entries(obj, lang)
        author_obj = getattr(obj, "author", None)
        author_name = ""
        if author_obj:
            author_name = (author_obj.get_full_name() or getattr(author_obj, "username", "") or "")

        json_ld = {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": title,
            "description": description,
            "url": canonical,
            "inLanguage": lang,
        }
        if entries:
            json_ld["about"] = [
                {
                    "@type": "SoftwareApplication",
                    "name": entry.tool.name,
                    "url": absolute_url(entry.tool.get_absolute_url()),
                }
                for entry in entries
            ]

        ctx["seo"] = self.build_seo(
            request,
            title=title,
            description=description,
            date=obj.updated_at,
            author=author_name,
            canonical=canonical,
            og_type="article",
            og_image=get_og_image(),
            alternates=alternates,
            json_ld=json_ld,
        )

        ctx["tool_entries"] = entries
        ctx["tools_list"] = [entry.tool for entry in entries]

        # Beta 11.9: the detail template rendered obj.title/intro/body -
        # raw parler descriptors, i.e. the current draft. These carry the
        # same published values the SEO block above already used.
        ctx["display_title"] = title
        ctx["display_intro"] = intro
        ctx["display_body"] = body

        ctx["categories"] = self._categories_for_object(obj)
        # Beta 8.11a: explicit language_code, matching related_guides()/
        # related_prompts()/related_usecases()' callers - see
        # related_comparisons()'s docstring for the leak this closes.
        rel_qs = related_comparisons(obj, limit=3, language_code=lang)
        ctx["related_comparisons"] = [to_teaser_item(c, "comparison") for c in rel_qs]

        ctx["crumbs"] = [
            (_("Comparisons"), reverse("compare:index")),
            (title, request.path),
        ]
        return ctx
