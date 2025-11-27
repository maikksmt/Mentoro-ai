# core/sitemaps.py

from django.contrib.sitemaps import Sitemap
from django.utils import timezone
from django.utils.translation import get_language

from catalog.models import Tool
from compare.models import Comparison
from glossary.models import GlossaryTerm
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

DEFAULT_LANG = "en"


class BasePublishableSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def lastmod(self, obj):
        for field in (
                "updated_at",
                "created_at",
                "published_at",
        ):
            value = getattr(obj, field, None)
            if value:
                return value
        return timezone.now()

    def location(self, obj):
        lang = get_language() or DEFAULT_LANG
        return obj.get_absolute_url(language=lang)


class GuideSitemap(BasePublishableSitemap):
    def items(self):
        return Guide.objects.published()


class PromptSitemap(BasePublishableSitemap):
    def items(self):
        return Prompt.objects.published()


class UseCaseSitemap(BasePublishableSitemap):
    def items(self):
        return UseCase.objects.published()


class ComparisonSitemap(BasePublishableSitemap):
    def items(self):
        return Comparison.objects.published()


class ToolSitemap(BasePublishableSitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self):
        manager = getattr(Tool, "published", None)
        return (manager or Tool.objects).all()


class GlossaryIndexSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.6

    def items(self):
        return ["index"]

    def location(self, item):
        lang = get_language() or DEFAULT_LANG
        return f"/{lang}/glossary/"


class GlossaryTermSitemap(Sitemap):
    priority = 0.7

    def items(self):
        lang = get_language() or DEFAULT_LANG
        manager = getattr(GlossaryTerm, "published", None)
        qs = (manager or GlossaryTerm.objects).filter(language=lang)
        return qs

    def lastmod(self, obj):
        for field in (
                "updated_at",
                "created_at",
                "published_at",
        ):
            value = getattr(obj, field, None)
            if value:
                return value
        return timezone.now()

    def location(self, obj):
        return obj.get_absolute_url()


class LegalSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.3

    def items(self):
        # Suffixe relativ zu /<lang>/legal/
        return ["legal-notice", "privacy", "cookies", "terms", "copyright"]

    def location(self, item):
        lang = get_language() or DEFAULT_LANG
        # -> /en/legal/privacy/, /de/legal/privacy/ etc.
        return f"/{lang}/legal/{item}/"
