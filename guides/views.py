from django.db.models import Prefetch, Q
from django.http import Http404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import ListView, DetailView

from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.services import related_guides, to_teaser_item
from core.views import SeoMixin
from .models import Guide, GuideSection, GuideItem


class GuideListView(SeoMixin, ListView):
    paginate_by = 15
    template_name = "guides/guide_list.html"
    context_object_name = "object_list"

    def get_queryset(self):
        lang = get_language()
        return (
            Guide.objects
            .visible_on_site()
            .active_translations(lang)
            .exclude(translations__slug__startswith="start-guide")
            .select_related("author", "reviewed_by")
            .prefetch_related("categories__translations", "tools__translations")
            .distinct()
            .order_by("-published_at", "-updated_at")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        lang = get_language()
        canonical = absolute_url(self.request.path)
        title = _("AI guides · MentoroAI")
        description = _(
            "Browse practical AI guides and tutorials to learn step by step how to use AI tools in everyday life and work."
        )
        alts = localized_alternates(self.request, "guides:list")
        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            canonical=canonical,
            alternates=alts,
            json_ld={
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": title,
                "description": description,
                "url": canonical,
                "inLanguage": lang,
            },
        )
        ctx["crumbs"] = [(_("Guides"), reverse("guides:list"))]
        return ctx


class GuideDetailView(SeoMixin, DetailView):
    model = Guide
    template_name = "guides/guide_detail.html"
    context_object_name = "object"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_object(self, queryset=None):
        slug = self.kwargs["slug"]
        obj = (
            Guide.objects
            .filter(Q(translations__public_slug=slug) | Q(translations__slug=slug))
            .distinct()
            .first()
        )
        if obj:
            return obj

        for g in Guide.objects.all():
            live = g.live_i18n or {}
            for data in live.values():
                if data.get("public_slug") == slug or data.get("slug") == slug:
                    return g
        raise Http404

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj: Guide = ctx["object"]
        lang = get_language()
        title = f"{obj.title}"
        desc_source = obj.intro or obj.body or obj.title
        desc = seo_text(desc_source)[:155]
        canonical = absolute_url(self.request.path)
        og_img = getattr(obj, "hero_image_url", None)
        author_obj = getattr(obj, "author", None)
        author_name = ""
        if author_obj:
            author_name = (author_obj.get_full_name() or getattr(author_obj, "username", "") or "")
        alts = localized_alternates(
            self.request,
            url_name="guides:detail",  # optional, Fallback
            obj=obj,
        )
        json_ld = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": title,
            "description": desc,
            "url": canonical,
            "inLanguage": lang,
            "mainEntityOfPage": canonical,

        }
        if og_img:
            json_ld["image"] = [og_img]

        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=desc,
            date=obj.updated_at,
            author=author_name,
            og_type="article",
            canonical=canonical,
            og_image=get_og_image(og_img),
            alternates=alts,
            json_ld=json_ld,
        )
        ctx["display_title"] = obj.display_title
        ctx["display_intro"] = obj.display_intro
        ctx["display_body"] = obj.display_body
        rel_qs = related_guides(obj, limit=3)
        ctx["related_guides"] = [to_teaser_item(g, "guide") for g in rel_qs]
        ctx["crumbs"] = [
            (_("Guides"), reverse("guides:list")),
            (obj.display_title, self.request.path),
        ]
        return ctx

    def get_queryset(self):
        lang = get_language()
        return (
            Guide.objects
            .active_translations(lang)
            .select_related("author")
            .prefetch_related(
                "categories__translations",
                "tools__translations",
                Prefetch(
                    "sections",
                    queryset=(
                        GuideSection.objects
                        .active_translations(lang)
                        .order_by("order")
                        .prefetch_related(
                            Prefetch(
                                "items",
                                queryset=(
                                    GuideItem.objects
                                    .active_translations(lang)
                                    .filter(is_published=True)
                                    .order_by("order")
                                    .distinct()
                                )
                            )
                        )
                        .distinct()
                    )
                )
            )
            .distinct()
        )
