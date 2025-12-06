# usecases/views.py
from typing import Any, Dict, Optional

from django.db.models import Q, QuerySet
from django.http import Http404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import ListView, DetailView

from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.services import related_usecases, to_teaser_item
from core.views import SeoMixin
from .models import UseCase


def _resolve_by_slug(qs: QuerySet[UseCase], slug: str) -> Optional[UseCase]:
    obj = (
        qs.filter(Q(translations__public_slug=slug) | Q(translations__slug=slug))
        .distinct()
        .first()
    )
    if obj:
        return obj

    for u in qs:
        live = (u.live_i18n or {})
        for data in live.values():
            if data.get("public_slug") == slug or data.get("slug") == slug:
                return u
    return None


class UseCaseListView(SeoMixin, ListView):
    paginate_by = 15
    template_name = "usecases/list.html"
    context_object_name = "object_list"

    def get_queryset(self) -> QuerySet[UseCase]:
        lang = get_language()
        return (
            UseCase.published
            .active_translations(lang)
            .select_related("author", "reviewed_by")
            .prefetch_related("tools")
            .distinct()
            .order_by("-published_at", "-updated_at")
        )

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        lang = get_language()
        canonical = absolute_url(self.request.path)
        alts = localized_alternates(self.request, "usecases:list")
        title = _("AI use cases · MentoroAI")
        description = _(
            "Practical AI use cases with clear examples, benefits and guidance for business tasks, research and creative work."
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
        ctx["crumbs"] = [(_("Usecases"), self.request.path)]
        return ctx


class UseCaseDetailView(SeoMixin, DetailView):
    model = UseCase
    template_name = "usecases/detail.html"
    context_object_name = "object"

    def get_queryset(self) -> QuerySet[UseCase]:
        return UseCase.objects.all().select_related("author", "reviewed_by").prefetch_related("tools")

    def get_object(self, queryset: Optional[QuerySet[UseCase]] = None) -> UseCase:
        slug = self.kwargs.get("slug")
        if not slug:
            raise Http404("Missing slug.")
        qs = queryset or self.get_queryset()
        obj = _resolve_by_slug(qs, slug)
        if not obj:
            raise Http404("UseCase not found.")
        return obj

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        obj: UseCase = ctx["object"]
        lang = get_language()
        title = getattr(obj, "display_title", None) or getattr(obj, "title", None) or str(obj)
        desc_source = getattr(obj, "display_intro", None) or getattr(obj, "display_body", None) or title
        desc = seo_text(desc_source)[:155]
        canonical = absolute_url(obj.get_absolute_url())
        og_img = getattr(obj, "hero_image_url", None)
        alts = localized_alternates(
            self.request,
            url_name="usecases:detail",
            obj=obj,
        )
        json_ld = {
            "@context": "https://schema.org",
            "@type": "CaseStudy",
            "name": title,
            "description": desc,
            "url": canonical,
            "inLanguage": lang,
            "mainEntityOfPage": canonical,
        }
        tools = list(getattr(obj, "tools").all()) if hasattr(obj, "tools") else []
        if tools:
            json_ld["about"] = [
                {
                    "@type": "SoftwareApplication",
                    "name": t.name,
                    "url": absolute_url(t.get_absolute_url()),
                }
                for t in tools
            ]
        if og_img:
            json_ld["image"] = [og_img]

        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=desc,
            canonical=canonical,
            og_image=get_og_image(og_img),
            alternates=alts,
            json_ld=json_ld,
        )
        ctx.setdefault("display_title", obj.display_title)
        ctx.setdefault("display_intro", obj.display_intro)
        ctx.setdefault("display_body", obj.display_body)
        ctx.setdefault("display_outro", obj.display_outro)
        ctx.setdefault("display_persona", obj.display_persona)
        rel_qs = related_usecases(obj, limit=3)
        ctx["similar"] = [to_teaser_item(u, "usecase") for u in rel_qs]
        ctx["crumbs"] = [
            (_("Usecases"), reverse("usecases:list")),
            (obj.display_title or _("Usecase"), self.request.path),
        ]
        ctx["lastmod"] = getattr(obj, "updated_at", None) or getattr(obj, "published_at", None) or getattr(obj,
                                                                                                           "created_at",
                                                                                                           None)
        return ctx
