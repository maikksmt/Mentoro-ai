"""
Beta 8.10 Section G: related_guides() must be language-isolated.

Confirmed empirically before the fix: an EN-only guide could rank as the
top "related" result on a German guide's page (via shared category), using
Guide.published's ambient, fallback-inclusive active_translations(). Fixed
by switching the base queryset to Guide.objects.visible_in_language(lang)
(explicit language parameter). Ranking weights/order and limit are
unchanged; only the base visibility queryset, language filter, and
duplicate-avoidance were touched.
"""
from django.test import TestCase
from django.utils import timezone
from parler.utils.context import switch_language

from catalog.models import Category
from core.models.editorial import EditorialWorkflowMixin
from core.services import related_guides
from guides.models import Guide


def make_guide(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=None, languages=("en", "de"), **extra):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        g.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return g


def make_category(*, slug, name="Cat"):
    cat = Category.objects.create(slug=slug)
    with switch_language(cat, "en"):
        cat.name = name
        cat.save()
    return cat


class RelatedGuidesLanguageIsolationTests(TestCase):
    def test_en_only_related_absent_under_de(self):
        cat = make_category(slug="rel-cat-1")
        current = make_guide(slug="current-de-rel", languages=("de",))
        current.categories.add(cat)
        en_only = make_guide(slug="en-only-related-guide", languages=("en",))
        en_only.categories.add(cat)

        result = related_guides(current, limit=6, language_code="de")
        self.assertNotIn(en_only.pk, [g.pk for g in result])

    def test_de_only_related_absent_under_en(self):
        cat = make_category(slug="rel-cat-2")
        current = make_guide(slug="current-en-rel", languages=("en",))
        current.categories.add(cat)
        de_only = make_guide(slug="de-only-related-guide", languages=("de",))
        de_only.categories.add(cat)

        result = related_guides(current, limit=6, language_code="en")
        self.assertNotIn(de_only.pk, [g.pk for g in result])

    def test_bilingual_related_appears_in_both_languages(self):
        cat = make_category(slug="rel-cat-3")
        current_en = make_guide(slug="current-en-rel-2", languages=("en",))
        current_de = make_guide(slug="current-de-rel-2", languages=("de",))
        both = make_guide(slug="both-related-guide", languages=("en", "de"))
        for g in (current_en, current_de, both):
            g.categories.add(cat)

        self.assertIn(both.pk, [g.pk for g in related_guides(current_en, limit=6, language_code="en")])
        self.assertIn(both.pk, [g.pk for g in related_guides(current_de, limit=6, language_code="de")])

    def test_related_guide_with_active_translation_appears(self):
        cat = make_category(slug="rel-cat-4")
        current = make_guide(slug="current-appear", languages=("en",))
        current.categories.add(cat)
        match = make_guide(slug="match-appear", languages=("en",))
        match.categories.add(cat)

        result = related_guides(current, limit=6, language_code="en")
        self.assertIn(match.pk, [g.pk for g in result])

    def test_current_guide_excluded_from_its_own_recommendations(self):
        cat = make_category(slug="rel-cat-5")
        current = make_guide(slug="self-exclude-guide", languages=("en",))
        current.categories.add(cat)
        make_guide(slug="filler-guide-1", languages=("en",))

        result = related_guides(current, limit=6, language_code="en")
        self.assertNotIn(current.pk, [g.pk for g in result])

    def test_draft_never_recommended(self):
        current = make_guide(slug="current-no-draft", languages=("en",))
        make_guide(slug="draft-related-guide", status=EditorialWorkflowMixin.STATUS_DRAFT,
                   published_at=None, languages=("en",))
        result = related_guides(current, limit=6, language_code="en")
        self.assertEqual(len(result), 0)

    def test_existing_limit_is_preserved(self):
        current = make_guide(slug="current-limit-guide", languages=("en",))
        for i in range(10):
            make_guide(slug=f"limit-filler-guide-{i}", languages=("en",))
        result = related_guides(current, limit=3, language_code="en")
        self.assertLessEqual(len(result), 3)

    def test_no_duplicates(self):
        cat = make_category(slug="rel-cat-6")
        current = make_guide(slug="current-nodup-guide", languages=("en",))
        current.categories.add(cat)
        related = make_guide(slug="nodup-related-guide", languages=("en",))
        related.categories.add(cat)

        result = related_guides(current, limit=6, language_code="en")
        pks = [g.pk for g in result]
        self.assertEqual(len(pks), len(set(pks)))

    def test_all_related_links_return_200_in_active_language(self):
        cat = make_category(slug="rel-cat-7")
        current = make_guide(slug="current-links-guide", languages=("en",))
        current.categories.add(cat)
        for i in range(3):
            g = make_guide(slug=f"links-related-guide-{i}", languages=("en",))
            g.categories.add(cat)

        result = related_guides(current, limit=6, language_code="en")
        for g in result:
            resp = self.client.get(g.get_absolute_url(language="en"))
            self.assertEqual(resp.status_code, 200)


class RelatedGuidesWidgetOnDetailPageTests(TestCase):
    def test_detail_page_related_widget_has_no_wrong_language_links(self):
        cat = make_category(slug="rel-cat-widget")
        current = make_guide(slug="widget-current-guide", languages=("de",))
        current.categories.add(cat)
        en_only = make_guide(slug="widget-en-only-guide", languages=("en",))
        en_only.categories.add(cat)
        de_related = make_guide(slug="widget-de-related-guide", languages=("de",))
        de_related.categories.add(cat)

        resp = self.client.get("/de/guides/widget-current-guide-de/")
        self.assertEqual(resp.status_code, 200)
        related = resp.context["related_guides"]
        for item in related:
            self.assertTrue(item["url"].startswith("/de/"))
            check = self.client.get(item["url"])
            self.assertEqual(check.status_code, 200)
