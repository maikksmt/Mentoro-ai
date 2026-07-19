# content/views/home.py
from django.utils.translation import gettext as _, get_language
from django.views.generic import TemplateView

from catalog.models import Tool
from core.seo.utils import absolute_url, localized_alternates, get_og_image
from core.services import (
    get_latest_items,
    resolve_public_starter_guide,
)
from core.views import SeoMixin
from mentoroai import settings


def resolve_starter_guide_url(lang: str) -> str | None:
    """
    Thin wrapper around the shared starter-guide resolution (also used by the
    global footer via get_public_inventory) that keeps this view's existing
    public function name/signature stable.
    """
    starter = resolve_public_starter_guide(lang)
    return starter["url"] if starter else None


class HomePageView(SeoMixin, TemplateView):
    template_name = "content/home.html"

    def get_context_data(self, **kwargs):
        lang = (get_language() or "en")[:2]
        ctx = super().get_context_data(**kwargs)
        canonical = absolute_url(self.request.path)
        alts = localized_alternates(self.request, "content:home")
        title = _("AI tools, guides & usecases for beginners and professionals")
        description = _(
            "MentoroAI offers AI tutorials, guides, prompts, tool comparisons, use cases and a glossary to help you navigate the modern AI world."
        )
        # anchor = Guide.published.order_by("-published_at").first()
        # recommended_items = (
        #     [to_teaser_item(g, "guide") for g in related_guides(anchor, limit=3)]
        #     if anchor
        #     else []
        # )
        json_ld = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "@id": canonical + "#website",
            "url": canonical,
            "name": getattr(settings, "SITE_NAME", "MentoroAI"),
            "inLanguage": lang,
            "description": description,
        }

        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            og_type="website",
            canonical=canonical,
            alternates=alts,
            og_image=get_og_image(),
            json_ld=json_ld,
        )
        ctx["start_guide_url"] = resolve_starter_guide_url(lang)
        ctx["latest_items"] = get_latest_items(limit=6)
        ctx["featured_tools"] = Tool.objects.filter(is_featured=True).order_by(
            "-published_at"
        )[:6]

        # ctx["recommended_items"] = recommended_items
        return ctx
