from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import ListView, DetailView

from catalog.models import Category
from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.services import related_comparisons, to_teaser_item
from core.views import SeoMixin
from .models import Comparison


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
        lang = get_language()
        slug = self.kwargs["slug"]

        qs = (
            Comparison.published.language(lang)
            .prefetch_related(
                "tools",
                "tool_entries",
                "tool_entries__tool",
            )
            .distinct()
        )

        # Beta 8.8: match the slug on the active-language translation
        # specifically - qs already requires *some* (possibly fallback)
        # translation via Comparison.published, but an unqualified slug
        # match could otherwise hit a different language's row on the same
        # object, silently serving e.g. an English page under /de/.
        try:
            return qs.get(translations__language_code=lang, translations__public_slug=slug)
        except Comparison.DoesNotExist:
            return get_object_or_404(qs, translations__language_code=lang, translations__slug=slug)

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

    def _related(self, obj: Comparison, limit: int = 4):
        """
        Simple related comparisons: share at least one tool or category.
        """
        lang = get_language()

        tools = obj.tools.all()
        categories = self._categories_for_object(obj)

        qs = (
            Comparison.published.language(lang)
            .exclude(pk=obj.pk)
            .filter(
                Q(tools__in=tools)
                | Q(tools__categories__in=categories)
            )
            .distinct()
        )
        return qs[:limit]

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
        rel_qs = related_comparisons(obj, limit=3)
        ctx["related_comparisons"] = [to_teaser_item(c, "comparison") for c in rel_qs]

        ctx["crumbs"] = [
            (_("Comparisons"), reverse("compare:index")),
            (title, request.path),
        ]
        return ctx
