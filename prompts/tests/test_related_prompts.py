"""
Beta 8.9: related_prompts() and the "Related Prompts" widget on Prompt
detail pages must be strictly language-isolated (visible_in_language()),
matching PromptDetailView's own strict Beta 8.8 resolution - a related-
prompt link must never 404 under the active language.
"""
from django.test import TestCase
from django.utils import timezone, translation

from core.models.editorial import EditorialWorkflowMixin
from core.services import related_prompts
from prompts.models import Prompt


def make_prompt(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                 published_at=None, languages=("en", "de"), **extra):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    p = Prompt.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        p.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return p


class RelatedPromptsQuerySetTests(TestCase):
    def test_related_prompt_with_active_translation_appears_en(self):
        current = make_prompt(slug="current", languages=("en",))
        other = make_prompt(slug="other", languages=("en",))
        result = related_prompts(current, language_code="en")
        self.assertIn(other, result)

    def test_related_prompt_with_active_translation_appears_de(self):
        current = make_prompt(slug="current-de", languages=("de",))
        other = make_prompt(slug="other-de", languages=("de",))
        result = related_prompts(current, language_code="de")
        self.assertIn(other, result)

    def test_related_prompt_without_active_translation_is_absent_en(self):
        current = make_prompt(slug="current-2", languages=("en",))
        de_only = make_prompt(slug="de-only", languages=("de",))
        result = related_prompts(current, language_code="en")
        self.assertNotIn(de_only, result)

    def test_related_prompt_without_active_translation_is_absent_de(self):
        current = make_prompt(slug="current-3", languages=("de",))
        en_only = make_prompt(slug="en-only", languages=("en",))
        result = related_prompts(current, language_code="de")
        self.assertNotIn(en_only, result)

    def test_current_prompt_excluded_from_its_own_recommendations(self):
        current = make_prompt(slug="self-exclude", languages=("en",))
        make_prompt(slug="filler-1", languages=("en",))
        make_prompt(slug="filler-2", languages=("en",))
        result = related_prompts(current, language_code="en")
        self.assertNotIn(current.pk, [p.pk for p in result])

    def test_draft_is_never_recommended(self):
        current = make_prompt(slug="current-4", languages=("en",))
        make_prompt(slug="draft-related", status=EditorialWorkflowMixin.STATUS_DRAFT,
                    published_at=None, languages=("en",))
        result = related_prompts(current, language_code="en")
        self.assertEqual(len(result), 0)

    def test_review_with_live_revision_can_still_be_recommended(self):
        current = make_prompt(slug="current-5", languages=("en",))
        # Beta 11.11D1: see test_language_visibility - is_published plus a
        # real snapshot is the publication proof now.
        make_prompt(slug="review-live", status=EditorialWorkflowMixin.STATUS_REVIEW,
                    published_at=None, languages=("en",), last_published_revision_id=1,
                    is_published=True,
                    live_i18n={"en": {"title": "Live", "slug": "review-live-en"}})
        result = related_prompts(current, language_code="en")
        self.assertEqual(len(result), 1)

    def test_bilingual_related_prompt_appears_in_both_languages(self):
        current_en = make_prompt(slug="current-en", languages=("en",))
        current_de = make_prompt(slug="current-de-2", languages=("de",))
        both = make_prompt(slug="both", languages=("en", "de"))
        self.assertIn(both, related_prompts(current_en, language_code="en"))
        self.assertIn(both, related_prompts(current_de, language_code="de"))

    def test_no_related_link_404s_under_active_language(self):
        # get_absolute_url()'s own reverse() call uses Django's ambient
        # active language for the URL prefix (a separate, pre-existing,
        # dormant quirk in all four editorial models - real callers always
        # wrap it in translation.override(lang) first, exactly like this).
        current = make_prompt(slug="current-6", languages=("en",))
        for i in range(5):
            make_prompt(slug=f"related-{i}", languages=("en",))
        result = related_prompts(current, language_code="en", limit=6)
        with translation.override("en"):
            for p in result:
                resp = self.client.get(p.get_absolute_url(language="en"))
                self.assertEqual(resp.status_code, 200)

    def test_existing_limit_is_preserved(self):
        current = make_prompt(slug="current-7", languages=("en",))
        for i in range(10):
            make_prompt(slug=f"limit-{i}", languages=("en",))
        result = related_prompts(current, language_code="en", limit=3)
        self.assertLessEqual(len(result), 3)

    def test_no_duplicates(self):
        current = make_prompt(slug="current-8", languages=("en",))
        for i in range(4):
            make_prompt(slug=f"nodup-{i}", languages=("en",))
        result = related_prompts(current, language_code="en")
        pks = [p.pk for p in result]
        self.assertEqual(len(pks), len(set(pks)))


class PromptDetailRelatedWidgetTests(TestCase):
    def test_detail_page_related_widget_has_no_wrong_language_links(self):
        make_prompt(slug="widget-current", languages=("de",))
        make_prompt(slug="widget-en-only", languages=("en",))
        make_prompt(slug="widget-de-related", languages=("de",))

        resp = self.client.get("/de/prompts/widget-current-de/")
        self.assertEqual(resp.status_code, 200)
        more = resp.context["more"]
        for item in more:
            self.assertTrue(item["url"].startswith("/de/"))
            check = self.client.get(item["url"])
            self.assertEqual(check.status_code, 200)
