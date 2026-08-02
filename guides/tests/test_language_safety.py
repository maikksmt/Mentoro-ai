"""
Beta 8.10: GuideListView/GuideDetailView must be status- and language-safe.

Confirmed empirically (before the fix):
- GuideDetailView.get_object() used Guide.objects.filter(Q(translations__
  public_slug=slug) | Q(translations__slug=slug)) with NO status filter and
  NO language restriction (matched a slug in ANY translation, any status).
  A draft was publicly resolvable by direct slug URL.
- A DE-only guide requested under /en/<de-slug>/ crashed with
  parler.models.DoesNotExist: "Guide does not have a translation for the
  current language!" - raised from guides/views.py:87 (`title = f"{obj.title}"`,
  the raw Parler descriptor, not the live-snapshot-safe display_title).
- An EN-only guide requested under /de/<en-slug>/ returned HTTP 200 and
  silently rendered the English title/body under a German URL prefix
  (<html lang="de"> with English text) - no 404, wrong content.

Both are fixed via Guide.objects.visible_in_language(lang) (strict status +
language) as the base for get_queryset(), and a strict, language-scoped
_resolve_guide_by_slug() (mirroring Prompt/UseCase's Beta 8.8/8.9a pattern)
for get_object().
"""
from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide


def make_guide(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=None, languages=("en", "de"), **extra):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        g.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return g


# ---------------------------------------------------------------------------
# Section A: reproduction (fixed behavior asserted directly - the historical
# 500/wrong-render is documented in the report and in the docstring above,
# reproduced via shell before this fix was implemented).
# ---------------------------------------------------------------------------

class GuideDetailReproductionTests(TestCase):
    def test_de_only_guide_under_en_returns_404_not_500(self):
        make_guide(slug="de-only-repro", languages=("de",))
        resp = self.client.get("/en/guides/de-only-repro-de/")
        self.assertEqual(resp.status_code, 404)

    def test_en_only_guide_under_de_returns_404_not_wrong_language_200(self):
        make_guide(slug="en-only-repro", languages=("en",))
        resp = self.client.get("/de/guides/en-only-repro-en/")
        self.assertEqual(resp.status_code, 404)

    def test_draft_guide_direct_url_returns_404(self):
        make_guide(slug="draft-repro", status=EditorialWorkflowMixin.STATUS_DRAFT,
                   published_at=None, languages=("en",))
        resp = self.client.get("/en/guides/draft-repro-en/")
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Section B: GuideQuerySet.visible_in_language()
# ---------------------------------------------------------------------------

class GuideVisibleInLanguageTests(TestCase):
    def test_published_guide_with_active_translation_returned_en(self):
        g = make_guide(slug="active-en", languages=("en",))
        self.assertIn(g, Guide.objects.visible_in_language("en"))

    def test_published_guide_with_active_translation_returned_de(self):
        g = make_guide(slug="active-de", languages=("de",))
        self.assertIn(g, Guide.objects.visible_in_language("de"))

    def test_en_only_guide_absent_from_de_queryset(self):
        g = make_guide(slug="en-only-strict", languages=("en",))
        self.assertNotIn(g, Guide.objects.visible_in_language("de"))

    def test_de_only_guide_absent_from_en_queryset(self):
        g = make_guide(slug="de-only-strict", languages=("de",))
        self.assertNotIn(g, Guide.objects.visible_in_language("en"))

    def test_bilingual_guide_visible_in_both_languages(self):
        g = make_guide(slug="bilingual-strict", languages=("en", "de"))
        self.assertIn(g, Guide.objects.visible_in_language("en"))
        self.assertIn(g, Guide.objects.visible_in_language("de"))

    def test_draft_never_visible(self):
        g = make_guide(slug="draft-strict", status=EditorialWorkflowMixin.STATUS_DRAFT,
                        published_at=None, languages=("en", "de"))
        self.assertNotIn(g, Guide.objects.visible_in_language("en"))
        self.assertNotIn(g, Guide.objects.visible_in_language("de"))

    def test_review_with_live_revision_visible_per_visible_on_site(self):
        # Beta 11.11D1: a past publication is proven by is_published plus a
        # real live snapshot, not by the legacy last_published_revision_id
        # marker (which only core.admin's publish path ever writes).
        g = make_guide(slug="review-live-strict", status=EditorialWorkflowMixin.STATUS_REVIEW,
                        published_at=None, languages=("en",), last_published_revision_id=1,
                        is_published=True,
                        live_i18n={"en": {"title": "Live", "slug": "review-live-strict-en"}})
        self.assertIn(g, Guide.objects.visible_in_language("en"))

    def test_review_without_live_revision_not_visible(self):
        g = make_guide(slug="review-nolive-strict", status=EditorialWorkflowMixin.STATUS_REVIEW,
                        published_at=None, languages=("en",))
        self.assertNotIn(g, Guide.objects.visible_in_language("en"))

    def test_no_duplicates_from_translation_join(self):
        for i in range(5):
            make_guide(slug=f"nodup-strict-{i}", languages=("en", "de"))
        qs = Guide.objects.visible_in_language("en")
        pks = list(qs.values_list("pk", flat=True))
        self.assertEqual(len(pks), len(set(pks)))

    def test_active_parler_language_is_set_for_property_access(self):
        make_guide(slug="lang-set-check", languages=("en", "de"))
        g = Guide.objects.visible_in_language("de").get(translations__slug="lang-set-check-de")
        self.assertEqual(g.get_current_language(), "de")
        self.assertEqual(g.title, "Title lang-set-check de")

    def test_arbitrary_slug_values_do_not_affect_language_check(self):
        make_guide(slug="weird-slug-123_ABC", languages=("en",))
        self.assertTrue(Guide.objects.visible_in_language("en").filter(
            translations__slug="weird-slug-123_ABC-en").exists())
        self.assertFalse(Guide.objects.visible_in_language("de").filter(
            translations__slug="weird-slug-123_ABC-en").exists())


# ---------------------------------------------------------------------------
# Section C: GuideListView
# ---------------------------------------------------------------------------

class GuideListLanguageIsolationTests(TestCase):
    def _list(self, lang):
        return self.client.get(f"/{lang}/guides/")

    def test_en_only_absent_from_german_list(self):
        make_guide(slug="en-only-list", languages=("en",))
        objs = list(self._list("de").context["object_list"])
        self.assertFalse(any("en-only-list" in g.slug for g in objs))

    def test_de_only_absent_from_english_list(self):
        make_guide(slug="de-only-list", languages=("de",))
        objs = list(self._list("en").context["object_list"])
        self.assertFalse(any("de-only-list" in g.slug for g in objs))

    def test_bilingual_guide_appears_in_both_lists(self):
        make_guide(slug="bilingual-list", languages=("en", "de"))
        en_objs = list(self._list("en").context["object_list"])
        de_objs = list(self._list("de").context["object_list"])
        self.assertTrue(any(g.slug == "bilingual-list-en" for g in en_objs))
        self.assertTrue(any(g.slug == "bilingual-list-de" for g in de_objs))

    def test_draft_absent_from_every_language_list(self):
        make_guide(slug="draft-list", status=EditorialWorkflowMixin.STATUS_DRAFT,
                   published_at=None, languages=("en", "de"))
        en_objs = list(self._list("en").context["object_list"])
        de_objs = list(self._list("de").context["object_list"])
        self.assertFalse(any("draft-list" in g.slug for g in en_objs))
        self.assertFalse(any("draft-list" in g.slug for g in de_objs))

    def test_every_listed_guide_detail_url_returns_200(self):
        for i in range(4):
            make_guide(slug=f"reach-list-{i}", languages=("en",))
        objs = list(self._list("en").context["object_list"])
        for g in objs:
            resp = self.client.get(g.get_absolute_url(language="en"))
            self.assertEqual(resp.status_code, 200)

    def test_no_duplicate_results(self):
        for i in range(4):
            make_guide(slug=f"nodup-list-{i}", languages=("en", "de"))
        objs = list(self._list("en").context["object_list"])
        pks = [g.pk for g in objs]
        self.assertEqual(len(pks), len(set(pks)))

    def test_starter_appears_exactly_once_and_first(self):
        starter = make_guide(slug="starter-list-check", languages=("en",), is_starter=True)
        make_guide(slug="other-list-check", languages=("en",))
        objs = list(self._list("en").context["object_list"])
        self.assertEqual([g.pk for g in objs].count(starter.pk), 1)
        self.assertEqual(objs[0].pk, starter.pk)

    def test_pagination_still_works(self):
        for i in range(20):
            make_guide(slug=f"page-check-{i}", languages=("en",))
        resp = self._list("en")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("paginator", resp.context)
        self.assertEqual(resp.context["paginator"].per_page, 15)

    def test_empty_state_when_no_guides_in_language(self):
        make_guide(slug="only-de-for-empty-check", languages=("de",))
        resp = self._list("en")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Section D: GuideDetailView
# ---------------------------------------------------------------------------

class GuideDetailLanguageIsolationTests(TestCase):
    def test_en_only_under_en_returns_200(self):
        make_guide(slug="detail-en-only", languages=("en",))
        resp = self.client.get("/en/guides/detail-en-only-en/")
        self.assertEqual(resp.status_code, 200)

    def test_en_only_under_de_returns_404(self):
        make_guide(slug="detail-en-only-2", languages=("en",))
        resp = self.client.get("/de/guides/detail-en-only-2-en/")
        self.assertEqual(resp.status_code, 404)

    def test_de_only_under_de_returns_200(self):
        make_guide(slug="detail-de-only", languages=("de",))
        resp = self.client.get("/de/guides/detail-de-only-de/")
        self.assertEqual(resp.status_code, 200)

    def test_de_only_under_en_returns_404_not_500(self):
        make_guide(slug="detail-de-only-2", languages=("de",))
        resp = self.client.get("/en/guides/detail-de-only-2-de/")
        self.assertEqual(resp.status_code, 404)

    def test_bilingual_shows_correct_title_per_language(self):
        make_guide(slug="bilingual-detail", languages=("en", "de"))
        resp_en = self.client.get("/en/guides/bilingual-detail-en/")
        resp_de = self.client.get("/de/guides/bilingual-detail-de/")
        self.assertIn("Title bilingual-detail en", resp_en.content.decode())
        self.assertIn("Title bilingual-detail de", resp_de.content.decode())

    def test_bilingual_shows_correct_slug_per_language(self):
        make_guide(slug="bilingual-slug-detail", languages=("en", "de"))
        resp_en = self.client.get("/en/guides/bilingual-slug-detail-en/")
        resp_de = self.client.get("/de/guides/bilingual-slug-detail-de/")
        self.assertEqual(resp_en.status_code, 200)
        self.assertEqual(resp_de.status_code, 200)

    def test_wrong_language_slug_returns_404(self):
        make_guide(slug="wrong-slug-detail", languages=("en", "de"))
        # The EN slug requested under the DE prefix must not resolve.
        resp = self.client.get("/de/guides/wrong-slug-detail-en/")
        self.assertEqual(resp.status_code, 404)

    def test_draft_returns_404(self):
        make_guide(slug="draft-detail", status=EditorialWorkflowMixin.STATUS_DRAFT,
                   published_at=None, languages=("en",))
        resp = self.client.get("/en/guides/draft-detail-en/")
        self.assertEqual(resp.status_code, 404)

    def test_no_parler_doesnotexist_leaks_as_500(self):
        make_guide(slug="no-crash-detail", languages=("de",))
        # Historically this raised parler.models.DoesNotExist inside
        # get_context_data(); now it must 404 cleanly before that code runs.
        resp = self.client.get("/en/guides/no-crash-detail-de/")
        self.assertEqual(resp.status_code, 404)
        self.assertNotEqual(resp.status_code, 500)
