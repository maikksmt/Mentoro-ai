"""
Beta 8.8: Prompt.objects.visible_in_language() and the PromptListView /
PromptDetailView that now use it - public prompts must be strictly isolated
per active language, with zero cross-language fallback (unlike Guide/
UseCase, which intentionally allow an EN fallback via active_translations()).
"""
from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from prompts.models import Prompt


def make_prompt(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                 published_at=None, languages=("en", "de"), **extra_status_kwargs):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    p = Prompt.objects.create(status=status, published_at=published_at, **extra_status_kwargs)
    for lang in languages:
        p.create_translation(
            lang, title=f"Title {slug} {lang}", intro="i", body=f"body-{lang}",
            slug=f"{slug}-{lang}",
        )
    return p


class VisibleInLanguageQuerySetTests(TestCase):
    def test_published_prompt_with_active_translation_is_returned(self):
        p = make_prompt(slug="pub-en", languages=("en",))
        self.assertIn(p, Prompt.objects.visible_in_language("en"))

    def test_prompt_without_active_translation_is_not_returned(self):
        make_prompt(slug="de-only", languages=("de",))
        self.assertEqual(Prompt.objects.visible_in_language("en").count(), 0)

    def test_de_only_prompt_does_not_appear_in_english(self):
        make_prompt(slug="de-only-2", languages=("de",))
        self.assertEqual(Prompt.objects.visible_in_language("en").count(), 0)
        self.assertEqual(Prompt.objects.visible_in_language("de").count(), 1)

    def test_en_only_prompt_does_not_appear_in_german(self):
        make_prompt(slug="en-only-2", languages=("en",))
        self.assertEqual(Prompt.objects.visible_in_language("de").count(), 0)
        self.assertEqual(Prompt.objects.visible_in_language("en").count(), 1)

    def test_bilingual_prompt_appears_in_both_languages(self):
        make_prompt(slug="bilingual", languages=("en", "de"))
        self.assertEqual(Prompt.objects.visible_in_language("en").count(), 1)
        self.assertEqual(Prompt.objects.visible_in_language("de").count(), 1)

    def test_draft_is_excluded_from_every_language(self):
        make_prompt(slug="draft", status=EditorialWorkflowMixin.STATUS_DRAFT,
                    published_at=None, languages=("en", "de"))
        self.assertEqual(Prompt.objects.visible_in_language("en").count(), 0)
        self.assertEqual(Prompt.objects.visible_in_language("de").count(), 0)

    def test_review_with_live_revision_stays_visible_per_visible_on_site_rule(self):
        p = make_prompt(slug="review-live", status=EditorialWorkflowMixin.STATUS_REVIEW,
                         published_at=None, languages=("en",),
                         last_published_revision_id=1)
        self.assertIn(p, Prompt.objects.visible_in_language("en"))

    def test_review_without_live_revision_is_not_visible(self):
        make_prompt(slug="review-no-live", status=EditorialWorkflowMixin.STATUS_REVIEW,
                    published_at=None, languages=("en",))
        self.assertEqual(Prompt.objects.visible_in_language("en").count(), 0)

    def test_no_duplicate_results(self):
        make_prompt(slug="dup-check", languages=("en", "de"))
        qs = Prompt.objects.visible_in_language("en")
        self.assertEqual(qs.count(), len(list(qs)))

    def test_arbitrary_slug_does_not_affect_language_detection(self):
        p = make_prompt(slug="totally-unrelated-name", languages=("en",))
        self.assertIn(p, Prompt.objects.visible_in_language("en"))
        self.assertNotIn(p, Prompt.objects.visible_in_language("de"))


class PromptListViewLanguageTests(TestCase):
    def _list(self, lang):
        return self.client.get(f"/{lang}/prompts/")

    def test_english_list_shows_only_english_translations(self):
        make_prompt(slug="en-item", languages=("en",))
        make_prompt(slug="de-item", languages=("de",))
        objs = list(self._list("en").context["object_list"])
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0].safe_translation_getter("slug", language_code="en"), "en-item-en")

    def test_german_list_shows_only_german_translations(self):
        make_prompt(slug="en-item-2", languages=("en",))
        make_prompt(slug="de-item-2", languages=("de",))
        objs = list(self._list("de").context["object_list"])
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0].safe_translation_getter("slug", language_code="de"), "de-item-2-de")

    def test_bilingual_prompt_visible_in_both_lists(self):
        make_prompt(slug="both", languages=("en", "de"))
        self.assertEqual(len(self._list("en").context["object_list"]), 1)
        self.assertEqual(len(self._list("de").context["object_list"]), 1)

    def test_drafts_excluded_from_both_languages(self):
        make_prompt(slug="draft-list", status=EditorialWorkflowMixin.STATUS_DRAFT,
                    published_at=None, languages=("en", "de"))
        self.assertEqual(len(self._list("en").context["object_list"]), 0)
        self.assertEqual(len(self._list("de").context["object_list"]), 0)

    def test_existing_ordering_is_preserved(self):
        # PromptListView's own "-published_at, -updated_at" fallback never
        # actually applies: visible_on_site() (EditorialQuerySet) already
        # sets an ordering (plain "updated_at", ascending), so qs.ordered is
        # already True by the time the view checks it. This is unchanged,
        # pre-existing behavior (present before Beta 8.8 too) - out of
        # scope here per "bestehende Sortierung erhalten"; this test locks
        # in the real order rather than the one the code appears to intend.
        now = timezone.now()
        older = make_prompt(slug="older", languages=("en",), published_at=now - timezone.timedelta(days=2))
        newer = make_prompt(slug="newer", languages=("en",), published_at=now - timezone.timedelta(days=1))
        Prompt.objects.filter(pk=older.pk).update(updated_at=now - timezone.timedelta(days=2))
        Prompt.objects.filter(pk=newer.pk).update(updated_at=now - timezone.timedelta(days=1))
        objs = list(self._list("en").context["object_list"])
        self.assertEqual([o.pk for o in objs], [older.pk, newer.pk])

    def test_pagination_is_preserved(self):
        for i in range(20):
            make_prompt(slug=f"page-item-{i}", languages=("en",))
        resp = self._list("en")
        self.assertIn("paginator", resp.context)
        self.assertEqual(resp.context["paginator"].per_page, 15)

    def test_empty_state_still_renders(self):
        resp = self._list("en")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["object_list"]), [])


class PromptDetailViewLanguageTests(TestCase):
    def test_english_prompt_reachable_under_english_url(self):
        make_prompt(slug="en-detail", languages=("en",))
        resp = self.client.get("/en/prompts/en-detail-en/")
        self.assertEqual(resp.status_code, 200)

    def test_german_prompt_reachable_under_german_url(self):
        make_prompt(slug="de-detail", languages=("de",))
        resp = self.client.get("/de/prompts/de-detail-de/")
        self.assertEqual(resp.status_code, 200)

    def test_prompt_without_german_translation_404s_under_de(self):
        make_prompt(slug="en-only-detail", languages=("en",))
        resp = self.client.get("/de/prompts/en-only-detail-en/")
        self.assertEqual(resp.status_code, 404)

    def test_prompt_without_english_translation_404s_under_en(self):
        make_prompt(slug="de-only-detail", languages=("de",))
        resp = self.client.get("/en/prompts/de-only-detail-de/")
        self.assertEqual(resp.status_code, 404)

    def test_unpublished_prompt_is_public_404(self):
        make_prompt(slug="draft-detail", status=EditorialWorkflowMixin.STATUS_DRAFT,
                    published_at=None, languages=("en",))
        resp = self.client.get("/en/prompts/draft-detail-en/")
        self.assertEqual(resp.status_code, 404)

    def test_bilingual_prompt_shows_correct_translation_per_language(self):
        make_prompt(slug="bilingual-detail", languages=("en", "de"))
        resp_en = self.client.get("/en/prompts/bilingual-detail-en/")
        resp_de = self.client.get("/de/prompts/bilingual-detail-de/")
        self.assertEqual(resp_en.context["object"].safe_translation_getter("title", language_code="en"),
                          "Title bilingual-detail en")
        self.assertEqual(resp_de.context["object"].safe_translation_getter("title", language_code="de"),
                          "Title bilingual-detail de")

    def test_bilingual_prompt_english_slug_404s_under_german_prefix(self):
        # Different slugs per language: the EN slug must never resolve under /de/.
        make_prompt(slug="cross-slug", languages=("en", "de"))
        resp = self.client.get("/de/prompts/cross-slug-en/")
        self.assertEqual(resp.status_code, 404)

    def test_no_fallback_leak_in_title_body_or_breadcrumb(self):
        make_prompt(slug="no-leak", languages=("en",))
        resp = self.client.get("/de/prompts/no-leak-en/")
        self.assertEqual(resp.status_code, 404)
        # And the reverse direction: a DE-only prompt must not leak EN content.
        make_prompt(slug="no-leak-2", languages=("de",))
        resp2 = self.client.get("/en/prompts/no-leak-2-de/")
        self.assertEqual(resp2.status_code, 404)
