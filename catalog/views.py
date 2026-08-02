from django.db.models import Q
from django.http import Http404
from django.urls import reverse
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.generic import DetailView, ListView

from core.seo.utils import absolute_url, get_og_image, localized_alternates, seo_text
from core.views import SeoMixin

from .models import Category, Tool


class ToolListView(SeoMixin, ListView):
    model = Tool
    template_name = "catalog/tool_list.html"
    context_object_name = "object_list"
    paginate_by = 15

    def get_queryset(self):
        lang = get_language()
        q = self.request.GET.get("q") or ""
        free = self.request.GET.get("free") or ""
        category_slug = self.request.GET.get("category") or ""

        # Beta 8.14a: same rule as before, now via the central
        # ToolQuerySet.public() instead of an inline copy of the time filter.
        qs = (
            Tool.objects.public().language(lang)
            .prefetch_related("categories", "translations", "categories__translations")
        )

        if q:
            qs = qs.filter(
                Q(translations__name__icontains=q)
                | Q(translations__short_description__icontains=q)
                | Q(translations__long_description__icontains=q)
            )

        if free == "1" and hasattr(Tool, "free_tier"):
            qs = qs.filter(free_tier=True)

        if category_slug:
            qs = qs.filter(categories__translations__slug=category_slug)

        qs = qs.distinct()

        if hasattr(Tool, "updated_at"):
            qs = qs.order_by("-updated_at")
        elif hasattr(Tool, "created_at"):
            qs = qs.order_by("-created_at")
        else:
            qs = qs.order_by("pk")

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q") or ""
        free = self.request.GET.get("free") == "1"
        category_slug = self.request.GET.get("category") or ""
        lang = get_language()
        title = _("AI tools catalog")
        description = _(
            "Discover AI tools for productivity, marketing, coding, creatives and more to find the best match for your needs."
        )

        categories = (
            Category.objects
            .translated(lang)
            .order_by("translations__name")
        )
        active_cat = categories.filter(Q(translations__slug=category_slug)).first()

        canonical = absolute_url(self.request.path)
        alts = localized_alternates(self.request, "catalog:list")
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

        ctx.update(
            {
                "q": q,
                "free": free,
                "category": category_slug,
                "active_cat": active_cat,
                "categories": categories,
                "crumbs": [
                    (_("Catalog"), reverse("catalog:list")),
                    (_("All tools"), self.request.path),
                ],
            }
        )
        return ctx


class ToolDetailView(SeoMixin, DetailView):
    model = Tool
    template_name = "catalog/tool_detail.html"
    context_object_name = "object"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def _related_tools(self, obj: Tool):
        lang = get_language()
        qs = Tool.objects.public().language(lang).exclude(pk=obj.pk)
        qs = qs.filter(categories__in=obj.categories.all())
        return qs.distinct()[:3]

    def get_object(self, queryset=None):
        slug = self.kwargs["slug"]
        lang = get_language()
        # Beta 8.14a: .public() - this resolver previously had NO temporal
        # filter at all (unlike ToolListView/_related_tools/the inventory
        # count), so a tool scheduled for a future published_at was already
        # reachable under its public detail URL and returned HTTP 200.
        # Confirmed by reproduction before the fix. The language handling
        # below is untouched: slug is a shared field and the cross-language
        # fallback stays intentional (see test_language_fallback.py).
        obj = (
            Tool.objects
            .public()
            .language(lang)
            .filter(slug=slug)
            .distinct()
            .first()
        )

        if obj is None:
            raise Http404(_("Tool does not exist"))

        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj: Tool = ctx["object"]
        lang = get_language()

        title_ = obj.safe_translation_getter("name", any_language=True) or str(obj)
        title = f"{title_} · MentoroAI"
        outro = _("This AI tool enhances modern workflows and helps streamline key tasks across various use cases. ")

        if obj.short_description:
            desc_raw = obj.short_description + outro
        elif obj.name:
            desc_raw = obj.name + outro
        else:
            desc_raw = obj.long_description or outro

        desc = seo_text(desc_raw)[:155]
        canonical = absolute_url(self.request.path)
        og_img = getattr(obj, "hero_image_url", None)
        alts = localized_alternates(self.request, "catalog:detail", {"slug": obj.slug})
        json_ld = {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": title,
            "description": desc,
            "url": canonical,
            "inLanguage": lang,
        }
        if og_img:
            json_ld["image"] = [og_img]

        if getattr(obj, "website", None):
            json_ld["applicationCategory"] = "AIApplication"
        if getattr(obj, "vendor", None):
            json_ld["provider"] = {
                "@type": "Organization",
                "name": obj.vendor,
            }

        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=desc,
            date=obj.updated_at,
            canonical=canonical,
            og_image=get_og_image(og_img),
            alternates=alts,
            json_ld=json_ld,
        )

        ctx.update(
            {
                "related_tools": self._related_tools(obj),
                "crumbs": [
                    (_("Catalog"), reverse("catalog:list")),
                    (_("All tools"), reverse("catalog:list")),
                    (obj.safe_translation_getter("name", any_language=True), self.request.path),
                ],
            }
        )
        return ctx
