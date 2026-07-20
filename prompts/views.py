from typing import Any, Dict, Optional

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, Http404
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.generic import DetailView, ListView

from core.models.editorial import EditorialWorkflowMixin
from core.seo.utils import absolute_url, localized_alternates, seo_text, get_og_image
from core.services import to_teaser_item, related_prompts
from core.views import SeoMixin
from .models import Prompt


def _resolve_by_slug(qs: QuerySet[Prompt], slug: str, language_code: str) -> Optional[Prompt]:
    """
    Beta 8.11: mirrors guides/views.py::_resolve_guide_by_slug() - once a
    prompt has a live_i18n snapshot for language_code, that snapshot's slug
    is the SOLE public slug for that language; the current (possibly
    draft-in-progress) translation slug is not tried at all. Without this,
    a prompt mid-revision immediately exposed its new, unpublished slug the
    moment an editor changed it, independent of publish status - confirmed
    via reproduction in prompts/tests/test_draft_slug_leak.py.

    A narrow backward-compatibility fallback to the current translation's
    public_slug/slug applies ONLY to prompts that are strictly `published`
    AND have no live_i18n entry for this language at all (a historical
    record predating the live-snapshot mechanism). Review/approved prompts
    without a live snapshot for this language are excluded from that
    fallback - visible_in_language() already requires a genuine live
    revision for those statuses.

    Language matching is scoped throughout (translations__language_code=
    language_code), so a bilingual prompt's slug in the other language can
    never resolve under this prefix either.
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


class PromptListView(SeoMixin, ListView):
    model = Prompt
    template_name = "prompts/prompt_list.html"
    context_object_name = "object_list"
    paginate_by = 15

    def get_queryset(self) -> QuerySet[Prompt]:
        lang = get_language()
        qs = (
            Prompt.objects
            .visible_in_language(lang)
            .select_related("author", "reviewed_by")
        )
        if not qs.ordered:
            qs = qs.order_by("-published_at", "-updated_at")
        return qs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        lang = get_language()
        canonical = absolute_url(self.request.path)
        alts = localized_alternates(self.request, "prompts:list")
        title = _("AI prompts · MentoroAI")
        description = _("Browse ready-made AI prompts for writing, coding, learning, marketing and more.")
        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            og_type="website",
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
        ctx["crumbs"] = [
            (_("Prompts"), self.request.path),
        ]
        return ctx


class PromptDetailView(SeoMixin, DetailView):
    model = Prompt
    template_name = "prompts/prompt_detail.html"
    context_object_name = "object"

    def get_queryset(self) -> QuerySet[Prompt]:
        lang = get_language()
        return Prompt.objects.visible_in_language(lang).select_related("author", "reviewed_by")

    def get_object(self, queryset: Optional[QuerySet[Prompt]] = None) -> Prompt:
        slug = self.kwargs.get("slug")
        if not slug:
            raise Http404("Missing slug.")
        lang = get_language()
        qs = queryset or self.get_queryset()
        obj = _resolve_by_slug(qs, slug, lang)
        if not obj:
            raise Http404("Prompt not found.")
        return obj

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        lang = get_language()
        obj: Prompt = ctx["object"]
        title = obj.display_title or obj.title
        desc_source = obj.intro or obj.body or title
        desc = seo_text(desc_source)[:155]
        canonical = absolute_url(self.request.path)
        og_img = getattr(obj, "hero_image_url", None)
        author_obj = getattr(obj, "author", None)
        author_name = ""
        if author_obj:
            # get_full_name ist eine Methode → aufrufen!
            author_name = (author_obj.get_full_name() or getattr(author_obj, "username", "") or "")
        alts = localized_alternates(
            self.request,
            url_name="prompts:detail",
            obj=obj,
        )
        json_ld = {
            "@context": "https://schema.org",
            "@type": "CreativeWork",
            "name": title,
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
        ctx.setdefault("display_title", obj.display_title)
        ctx.setdefault("display_intro", obj.display_intro)
        ctx.setdefault("display_body", obj.display_body)
        ctx.setdefault("display_outro", obj.display_outro)

        rel_qs = related_prompts(obj, limit=3, language_code=lang)
        ctx["more"] = [to_teaser_item(p, "prompt") for p in rel_qs]

        ctx["crumbs"] = [
            (_("Prompts"), reverse("prompts:list")),
            (obj.display_title or _("Prompt"), self.request.path),
        ]
        return ctx


def prompt_list(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return PromptListView.as_view()(request, *args, **kwargs)


def prompt_detail(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return PromptDetailView.as_view()(request, *args, **kwargs)
