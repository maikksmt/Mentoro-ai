"""
Beta 8.14: the public sitemaps must be language-strict and must only ever
contain reachable public URLs.

Reproduced before the fix (core/sitemaps.py::BasePublishableSitemap):
every editorial sitemap returned `Model.objects.published()`, which is
language-independent, while location() reverses the object's URL under the
*active* i18n_patterns prefix. A single-language object was therefore
emitted in both /en/sitemap.xml and /de/sitemap.xml:

    http://example.com/de/guides/beta814-audit-g-enonly-en/    -> 404
    http://example.com/de/prompts/beta814-audit-p-enonly-en/   -> 404
    http://example.com/de/usecases/beta814-audit-u-enonly-en/  -> 404
    http://example.com#                                        -> malformed
                                                    (EN-only Comparison)

robots.txt advertises both language sitemaps, so these were served to
crawlers. The fix adds the language filter (translated()/language()) that
the list/detail/related/inventory surfaces already got in Beta 8.8-8.10;
the published()-only status rule is unchanged.
"""
import re

from django.test import TestCase
from django.utils import timezone

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

LOC_RE = re.compile(r"<loc>(.*?)</loc>")

EDITORIAL_MODELS = (
    ("guide", Guide),
    ("prompt", Prompt),
    ("usecase", UseCase),
    ("comparison", Comparison),
)


def make_published(model, slug, languages):
    obj = model.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
    )
    for lang in languages:
        obj.create_translation(
            lang, title=f"T {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}"
        )
    return obj


class SitemapMixin:
    def sitemap_locations(self, lang):
        resp = self.client.get(f"/{lang}/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        return LOC_RE.findall(resp.content.decode())

    def sitemap_paths(self, lang):
        paths = []
        for loc in self.sitemap_locations(lang):
            path = re.sub(r"^https?://[^/]+", "", loc)
            self.assertTrue(
                path.startswith("/"),
                f"sitemap emitted a malformed, non-path location: {loc!r}",
            )
            paths.append(path)
        return paths


class SitemapLanguageIsolationTests(SitemapMixin, TestCase):
    """Single-language content must appear only in its own language sitemap."""

    def test_en_only_content_is_absent_from_de_sitemap(self):
        for kind, model in EDITORIAL_MODELS:
            with self.subTest(kind=kind):
                make_published(model, f"sm-enonly-{kind}", ("en",))
        leaked = [u for u in self.sitemap_locations("de") if "sm-enonly-" in u]
        self.assertEqual(leaked, [], "EN-only content leaked into the DE sitemap")

    def test_de_only_content_is_absent_from_en_sitemap(self):
        for kind, model in EDITORIAL_MODELS:
            with self.subTest(kind=kind):
                make_published(model, f"sm-deonly-{kind}", ("de",))
        leaked = [u for u in self.sitemap_locations("en") if "sm-deonly-" in u]
        self.assertEqual(leaked, [], "DE-only content leaked into the EN sitemap")

    def test_en_only_content_is_present_in_en_sitemap(self):
        for kind, model in EDITORIAL_MODELS:
            with self.subTest(kind=kind):
                make_published(model, f"sm-present-{kind}", ("en",))
        locs = self.sitemap_locations("en")
        for kind, _model in EDITORIAL_MODELS:
            with self.subTest(kind=kind):
                self.assertTrue(
                    any(f"sm-present-{kind}-en" in u for u in locs),
                    f"{kind} missing from its own language sitemap",
                )

    def test_bilingual_content_appears_once_per_language_with_its_own_slug(self):
        for kind, model in EDITORIAL_MODELS:
            make_published(model, f"sm-bi-{kind}", ("en", "de"))
        for lang in ("en", "de"):
            locs = self.sitemap_locations(lang)
            for kind, _model in EDITORIAL_MODELS:
                with self.subTest(lang=lang, kind=kind):
                    hits = [u for u in locs if f"sm-bi-{kind}-{lang}" in u]
                    self.assertEqual(len(hits), 1, f"expected exactly one entry, got {hits}")
                    other = "de" if lang == "en" else "en"
                    self.assertFalse(
                        any(f"sm-bi-{kind}-{other}" in u for u in locs),
                        f"{other} slug leaked into the {lang} sitemap",
                    )


class SitemapDraftExclusionTests(SitemapMixin, TestCase):
    def test_never_published_drafts_are_absent_from_both_sitemaps(self):
        for kind, model in EDITORIAL_MODELS:
            obj = model.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
            obj.create_translation(
                "en", title="draft", intro="i", body="b", slug=f"sm-draft-{kind}-en"
            )
        for lang in ("en", "de"):
            locs = self.sitemap_locations(lang)
            with self.subTest(lang=lang):
                self.assertFalse(any("sm-draft-" in u for u in locs))


class SitemapReachabilityTests(SitemapMixin, TestCase):
    """Every URL the sitemap advertises must be a real HTTP 200 public page."""

    def setUp(self):
        make_published(Guide, "sm-reach-g-en", ("en",))
        make_published(Prompt, "sm-reach-p-en", ("en",))
        make_published(UseCase, "sm-reach-u-en", ("en",))
        make_published(Comparison, "sm-reach-c-en", ("en",))
        make_published(Guide, "sm-reach-g-de", ("de",))
        make_published(Prompt, "sm-reach-p-de", ("de",))
        make_published(UseCase, "sm-reach-u-de", ("de",))
        make_published(Comparison, "sm-reach-c-de", ("de",))
        make_published(Guide, "sm-reach-g-bi", ("en", "de"))
        make_published(Comparison, "sm-reach-c-bi", ("en", "de"))

    def test_every_sitemap_url_returns_200(self):
        for lang in ("en", "de"):
            paths = self.sitemap_paths(lang)
            self.assertTrue(paths, f"{lang} sitemap unexpectedly empty")
            for path in paths:
                with self.subTest(lang=lang, path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)

    def test_sitemap_contains_no_duplicate_urls(self):
        for lang in ("en", "de"):
            locs = self.sitemap_locations(lang)
            with self.subTest(lang=lang):
                self.assertEqual(
                    len(locs), len(set(locs)), f"duplicate loc entries in {lang} sitemap"
                )

    def test_sitemap_urls_carry_the_matching_language_prefix(self):
        for lang in ("en", "de"):
            for path in self.sitemap_paths(lang):
                with self.subTest(lang=lang, path=path):
                    self.assertTrue(
                        path.startswith(f"/{lang}/"),
                        f"{path} does not belong under the /{lang}/ prefix",
                    )
