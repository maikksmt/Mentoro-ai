from django.db.models import Q, QuerySet
from django.http import Http404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import ListView, DetailView

from catalog.models import Category
from core.models.editorial import EditorialWorkflowMixin
from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.services import related_comparisons, to_teaser_item
from core.views import SeoMixin
from .models import Comparison


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


class ComparisonListView(SeoMixin, ListView):
    model = Comparison
    template_name = "compare/comparison_list.html"
    context_object_name = "objects"
    paginate_by = 15

    def get_queryset(self):
        lang = get_language()
        q = self.request.GET.get("q") or ""

        # Beta 8.9: visible_in_language() (strict, no cross-language
        # fallback) instead of the .published manager's active_translations()
        # fallback - every card's detail URL must actually resolve under
        # the active language (see ComparisonDetailView's strict slug match
        # from Beta 8.8).
        qs = (
            Comparison.objects.visible_in_language(lang)
            .prefetch_related("tools", "tools__categories")
            .distinct()
        )

        cat = self.request.GET.get("category") or self.request.GET.get("cat")
        if cat:
            qs = qs.filter(
                Q(tools__categories__translations__slug=cat)
                | Q(tools__categories__pk__iexact=cat)
            ).distinct()

        if q:
            qs = qs.filter(
                Q(translations__title__icontains=q)
                | Q(translations__intro__icontains=q)
                | Q(translations__body__icontains=q)
                | Q(tools__translations__name__icontains=q)
            ).distinct()

        return qs

    def _categories_for_filters(self, ctx):
        lang = get_language()
        try:
            return (
                Category.objects
                .translated(lang)
                .filter(tools__comparisons__in=ctx["object_list"])
                .distinct()
                .order_by("translations__name")
            )
        except Exception:
            return Category.objects.translated(lang).order_by("name")

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

        ctx["categories"] = self._categories_for_filters(ctx)
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

        title = obj.get_live_value("title", language=lang) or obj.safe_translation_getter(
            "title"
        )
        intro = obj.get_live_value("intro", language=lang) or obj.safe_translation_getter(
            "intro"
        )
        body = obj.get_live_value("body", language=lang) or obj.safe_translation_getter(
            "body"
        )

        description_source = intro or body
        description = seo_text(description_source or "")[:155]

        canonical = absolute_url(obj.get_absolute_url(language=lang))
        alternates = localized_alternates(request, obj=obj)
        entries = obj.tool_entries.select_related("tool").all()
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
