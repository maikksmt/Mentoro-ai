"""
Beta 8.10 Section H: Guide-specific SEO alternates and language-switcher
regression, now that GuideDetailView resolves strictly per language.
"""
from django.template import Context
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from core.seo.utils import localized_alternates
from core.templatetags.i18n_next import i18n_next
from guides.models import Guide


def make_guide(*, slug, languages, status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at)
    for lang in languages:
        g.create_translation(lang, title=f"Guide {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return g


class GuideEnglishOnlySeoTests(TestCase):
    def test_detail_page_returns_200_under_en(self):
        make_guide(slug="seo-en-only", languages=("en",))
        resp = self.client.get("/en/guides/seo-en-only-en/")
        self.assertEqual(resp.status_code, 200)

    def test_en_alternate_present_de_absent(self):
        g = make_guide(slug="seo-en-only-2", languages=("en",))
        request = RequestFactory().get("/en/guides/seo-en-only-2-en/")
        alts = localized_alternates(request, obj=g)
        langs = {a.lang for a in alts}
        self.assertIn("en", langs)
        self.assertNotIn("de", langs)

    def test_no_wrong_de_link_in_alternate(self):
        g = make_guide(slug="seo-en-only-3", languages=("en",))
        request = RequestFactory().get("/en/guides/seo-en-only-3-en/")
        alts = localized_alternates(request, obj=g)
        for a in alts:
            if a.lang == "en":
                self.assertIn("/en/", a.url)
                self.assertNotIn("/de/", a.url)

    def test_language_switcher_does_not_500(self):
        make_guide(slug="seo-en-only-4", languages=("en",))
        resp = self.client.get("/en/guides/seo-en-only-4-en/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Server Error", resp.content.decode())


class GuideGermanOnlySeoTests(TestCase):
    def test_detail_page_returns_200_under_de(self):
        make_guide(slug="seo-de-only", languages=("de",))
        resp = self.client.get("/de/guides/seo-de-only-de/")
        self.assertEqual(resp.status_code, 200)

    def test_de_alternate_present_en_absent(self):
        g = make_guide(slug="seo-de-only-2", languages=("de",))
        request = RequestFactory().get("/de/guides/seo-de-only-2-de/")
        alts = localized_alternates(request, obj=g)
        langs = {a.lang for a in alts}
        self.assertIn("de", langs)
        self.assertNotIn("en", langs)

    def test_no_wrong_en_link_in_alternate(self):
        g = make_guide(slug="seo-de-only-3", languages=("de",))
        request = RequestFactory().get("/de/guides/seo-de-only-3-de/")
        alts = localized_alternates(request, obj=g)
        for a in alts:
            if a.lang == "de":
                self.assertIn("/de/", a.url)
                self.assertNotIn("/en/", a.url)

    def test_language_switcher_does_not_500(self):
        make_guide(slug="seo-de-only-4", languages=("de",))
        resp = self.client.get("/de/guides/seo-de-only-4-de/")
        self.assertEqual(resp.status_code, 200)


class GuideBilingualSeoTests(TestCase):
    def test_both_alternates_present_with_correct_slugs(self):
        g = make_guide(slug="seo-bilingual", languages=("en", "de"))
        request = RequestFactory().get("/en/guides/seo-bilingual-en/")
        alts = {a.lang: a.url for a in localized_alternates(request, obj=g)}
        self.assertTrue(alts["en"].endswith("/en/guides/seo-bilingual-en/"))
        self.assertTrue(alts["de"].endswith("/de/guides/seo-bilingual-de/"))

    def test_canonical_correct_per_language(self):
        make_guide(slug="seo-bilingual-2", languages=("en", "de"))
        resp_en = self.client.get("/en/guides/seo-bilingual-2-en/")
        resp_de = self.client.get("/de/guides/seo-bilingual-2-de/")
        self.assertIn("/en/guides/seo-bilingual-2-en/", resp_en.content.decode())
        self.assertIn("/de/guides/seo-bilingual-2-de/", resp_de.content.decode())

    def test_language_switch_leads_to_http_200(self):
        make_guide(slug="seo-bilingual-3", languages=("en", "de"))
        resp = self.client.get("/en/guides/seo-bilingual-3-en/")
        self.assertIn('data-next="/de/guides/seo-bilingual-3-de/"', resp.content.decode())
        follow = self.client.get("/de/guides/seo-bilingual-3-de/")
        self.assertEqual(follow.status_code, 200)

    def test_i18n_next_switches_to_correct_slug_directly(self):
        g = make_guide(slug="seo-bilingual-4", languages=("en", "de"))
        request = RequestFactory().get("/en/guides/seo-bilingual-4-en/")
        url = i18n_next(Context({"request": request, "object": g}), "de")
        self.assertTrue(url.endswith("/de/guides/seo-bilingual-4-de/"))
