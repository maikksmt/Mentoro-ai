from django.conf import settings
from django.utils.translation import gettext as _

from .types import SeoMeta
from .utils import absolute_url, get_og_image


def defaults(request) -> SeoMeta:
    canonical = absolute_url(request.path)
    return SeoMeta(
        title=getattr(settings, "SITE_NAME", "Site"),
        description=_(
            "MentoroAI provides curated AI guides, prompts, tool overviews, comparisons, and a clear glossary to help you work productively with modern AI technologies."),
        date="",
        author="",
        canonical=canonical,
        robots="index,follow",
        og_type="website",
        og_image=get_og_image(),
        alternates=[],
        json_ld={
            "@context": "https://schema.org",
            "@type": "WebSite",
            "url": getattr(settings, "SITE_URL", ""),
            "name": getattr(settings, "SITE_NAME", "Site"),
            "inLanguage": getattr(request, "LANGUAGE_CODE", "en")
        }
    )
