from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import ListView, DetailView

from catalog.models import Category
from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.views import SeoMixin
from .models import Comparison


class ComparisonListView(SeoMixin, ListView):
    model = Comparison
    template_name = "compare/index.html"
    context_object_name = "objects"
    paginate_by = 12

    def get_queryset(self):
        qs = Comparison.published.language().prefetch_related("tools", "tools__categories")
        cat = self.request.GET.get("category") or self.request.GET.get("cat")
        if cat:
            qs = qs.filter(
                Q(tools__categories__translations__slug=cat)
                | Q(tools__categories__pk__iexact=cat)
            ).distinct()

        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(
                Q(translations__title__icontains=q)
                | Q(translations__slug__icontains=q)
                | Q(translations__public_slug__icontains=q)
                | Q(tools__translations__name__icontains=q)
            ).distinct()
        if not qs.ordered:
            qs = qs.order_by("-published_at", "-updated_at")
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        lang = get_language()
        q = self.request.GET.get("q", "").strip()
        category = self.request.GET.get("category")
        available_categories = (
            Category.objects.filter(
                tools__comparisons__in=ctx["object_list"]
            )
            .active_translations(lang)
            .distinct()
            .order_by("translations__name")
        )

        canonical = absolute_url(self.request.path)
        alts = localized_alternates(self.request, "compare:index")
        title = _("AI tool comparisons · MentoroAI")
        description = _(
            "Compare AI tools by features, performance and use cases to find the best option for your workflows."
        )
        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            canonical=canonical,
            alternates=alts,
            og_image=get_og_image(),
            json_ld={
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": title,
                "description": description,
                "url": canonical,
                "inLanguage": lang,
            },
        )
        ctx.update({
            "crumbs": [
                (_("Comparisons"), self.request.path),
            ],
            "q": q or "",
            "category": category or "",
            "available_categories": available_categories,
        })
        return ctx


class ComparisonDetailView(SeoMixin, DetailView):
    model = Comparison
    template_name = "compare/detail.html"
    context_object_name = "object"

    def get_queryset(self):
        return Comparison.objects.language().prefetch_related("tools", "tools__categories")

    def get_object(self, queryset=None):
        slug = self.kwargs.get("slug")
        qs = queryset or self.get_queryset()

        obj = qs.filter(Q(translations__slug=slug)).distinct().first()

        if not obj:
            obj = get_object_or_404(
                Comparison.objects.prefetch_related("tools", "tools__categories"),
                Q(translations__slug=slug)
            )
        return obj

    def _build_score_rows(self, obj):
        sb = getattr(obj, "score_breakdown", None)
        if not sb:
            return []

        if isinstance(sb, dict):
            return [{"key": k, "value": v} for k, v in sb.items()]
        if isinstance(sb, list):
            rows = []
            for item in sb:
                if isinstance(item, dict):
                    key = item.get("key") or item.get("name") or item.get("title")
                    value = item.get("value") or item.get("score")
                    rows.append({"key": key, "value": value})
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    rows.append({"key": item[0], "value": item[1]})
            return rows
        return []

    def _related(self, obj):
        tools = getattr(obj, "tools", None)
        if not tools:
            return []
        tool_ids = list(tools.values_list("pk", flat=True))
        if not tool_ids:
            return []
        return (
            self.get_queryset()
            .filter(tools__in=tool_ids)
            .exclude(pk=obj.pk)
            .distinct()[:3]
        )

    def _categories_for_object(self, obj):
        try:
            return (
                Category.objects.language()
                .filter(tools__in=obj.tools.all())
                .distinct()
                .order_by("translations__name")
            )
        except Exception:
            return (
                Category.objects.filter(tools__in=obj.tools.all())
                .distinct()
                .order_by("name")
            )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj: Comparison = ctx["object"]
        title = obj.title
        desc_source = obj.intro or obj.title
        desc = seo_text(desc_source)[:155]
        canonical = absolute_url(obj.get_absolute_url())
        lang = get_language()
        og_img = get_og_image()
        alts = localized_alternates(
            self.request,
            url_name="compare:detail",  # optional, Fallback
            obj=obj,
        )
        tools = list(getattr(obj, "tools").all()) if getattr(obj, "tools", None) else []
        json_ld = {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": title,
            "description": desc,
            "inLanguage": lang,
            "mainEntityOfPage": canonical,
            "url": canonical,
            "itemListElement": [
                {
                    "@type": "SoftwareApplication",
                    "name": tool.name,
                    "url": absolute_url(tool.get_absolute_url()),
                    "position": idx + 1,
                }
                for idx, tool in enumerate(tools)
            ],
        }
        if og_img:
            json_ld["image"] = [og_img]

        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=desc,
            og_type="article",
            canonical=canonical,
            og_image=get_og_image(og_img),
            alternates=alts,
            json_ld=json_ld,
        )

        ctx.update({
            "categories": self._categories_for_object(obj),
            "tools_list": list(getattr(obj, "tools").all()) if getattr(obj, "tools", None) else [],
            "score_rows": self._build_score_rows(obj),
            "related": self._related(obj),

            "crumbs": [
                (_("Comparisons"), reverse("compare:index")),
                (title, self.request.path),
            ],
        })
        return ctx
