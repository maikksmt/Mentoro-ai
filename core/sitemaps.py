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
    """
    Beta 8.14: items() is language-strict.

    Previously every editorial sitemap returned `Model.objects.published()`,
    which is language-independent, while location() reverses
    obj.get_absolute_url(language=get_language()) under the active
    i18n_patterns prefix. An object published in only one language was
    therefore emitted in BOTH /en/sitemap.xml and /de/sitemap.xml, and the
    per-model get_absolute_url() fallbacks turned that into a broken public
    URL:

      - Guide/Prompt fall back to another language's slug (the
        `for lng in get_available_languages()` loop), producing e.g.
        "/de/guides/<en-slug>/" - a 404 under the language-strict detail
        views hardened in Beta 8.8-8.10.
      - UseCase falls back via parler's use_fallback=True descriptor, same
        result.
      - Comparison (hardened in Beta 8.11) correctly refuses to substitute
        and returns "#", which the sitemap framework emitted as the
        malformed loc "https://<domain>#".

    Both were confirmed by reproduction before this fix; robots.txt
    advertises both language sitemaps, so these URLs were served to
    crawlers.

    The status rule is deliberately unchanged (still published(), not the
    broader visible_on_site() used by some list views) - this only adds the
    language filter, exactly mirroring the visible_in_language() tightening
    applied to the list/detail/related/inventory surfaces in Beta 8.8-8.10.
    """

    changefreq = "weekly"
    priority = 0.8
    model = None

    def items(self):
        """
        Beta 11.11D1: the shared ``visible_in_language()`` contract - the same
        queryset the list, detail, related and search surfaces use - instead
        of the stricter ``published()`` this base carried before.

        ``UseCaseSitemap`` already made exactly this move in Beta 11.7 and
        documented the reasoning; D1 promotes it to the base because the gap
        it closes is not use-case-specific. The sitemap advertised only
        ``status=published`` rows while the site itself served
        ``visible_in_language()``, so any page kept online through an
        editorial round by its live snapshot was publicly reachable but
        absent from the sitemap - it dropped out and back in on every round.
        D1 makes that mismatch the common case rather than the exception,
        because every automatic invalidation now lands in ``draft``.

        The advertised URL is unaffected: ``location()`` resolves through
        ``get_absolute_url()``, which reads the live snapshot, so a row being
        edited keeps advertising its *published* slug, never the new draft
        one. ``visible_in_language()`` also carries the language gate, so a
        language without a published snapshot is still never advertised.
        """
        lang = get_language() or DEFAULT_LANG
        return self.model.objects.visible_in_language(lang)

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
    model = Guide


class PromptSitemap(BasePublishableSitemap):
    model = Prompt


class UseCaseSitemap(BasePublishableSitemap):
    #: Beta 11.7's local ``items()`` override moved into
    #: ``BasePublishableSitemap`` in Beta 11.11D1 - its reasoning was never
    #: use-case-specific, and Guide, Prompt and Comparison need it too. See
    #: the base method for the full contract.
    model = UseCase


class ComparisonSitemap(BasePublishableSitemap):
    model = Comparison

    def items(self):
        """
        Beta 11.9: the same visible_in_language() contract the comparison
        list, detail, related and search surfaces use - published, or
        review/approved/rework with both a live revision and a published
        entry snapshot, and only in languages that actually have a
        published snapshot.

        Overridden here rather than in BasePublishableSitemap because that
        base class is shared with Guide and Prompt, whose status rules are
        out of scope for this slice. The effect is that a comparison being
        revised keeps its sitemap entry (under its live slug) instead of
        dropping out and back in on every editing round.
        """
        lang = get_language() or DEFAULT_LANG
        return self.model.objects.visible_in_language(lang)


class ToolSitemap(BasePublishableSitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self):
        # Beta 8.14a: .public() - this previously returned every Tool
        # regardless of published_at (the getattr(Tool, "published") probe
        # never matched; Tool has no such manager), so a tool scheduled for
        # a future date was listed in both language sitemaps. That only
        # avoided being a broken URL because ToolDetailView had the same
        # gap and served it with HTTP 200; both are closed together.
        return Tool.objects.public()


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
