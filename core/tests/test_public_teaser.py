"""
Beta 8.10a Sections F/G: to_teaser_item() (and its helpers
_public_teaser_value()/_public_teaser_url()) must never surface an
unpublished draft title, intro/body, or slug for an editorial object mid
live-revision - confirmed for all four editorial content types that share
this helper (Guide, Prompt, UseCase, Comparison).

Root cause (Beta 8.10a): the previous _t() helper read
`safe_translation_getter(field, any_language=True)` - the CURRENT
(possibly draft-in-progress) translation row - and to_teaser_item()'s URL
generation reversed `obj.slug` directly (also the current translation),
completely bypassing the live_i18n snapshot that Guide/Prompt/UseCase's
own display_title/display_intro/display_body properties (and
get_absolute_url()) already respect.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.translation import get_language
from parler.utils.context import switch_language

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.services import to_teaser_item
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()


class TeaserLiveRevisionTestsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="teaser-live-editor", email="teaser-live@example.com", password="testpass123"
        )


def _begin_unpublished_revision(obj, *, title, intro, slug, author):
    obj.title = title
    obj.intro = intro
    obj.slug = slug
    obj.save()
    obj.move_to_review(by=author)
    obj.last_published_revision_id = 1
    obj.save()


class GuideTeaserLiveRevisionTests(TeaserLiveRevisionTestsBase):
    def test_published_state_shows_current_content(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="Guide Pub Title", intro="Guide pub intro", body="b",
                              slug="guide-teaser-pub")
        g.publish(by=self.author)
        g.save()

        item = to_teaser_item(g, "guide", language_code="en")
        self.assertEqual(item["title"], "Guide Pub Title")
        self.assertEqual(item["teaser"], "Guide pub intro")
        self.assertTrue(item["url"].endswith("/guides/guide-teaser-pub/"))

    def test_live_revision_shows_live_values_not_draft(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="Guide Live Title", intro="Guide live intro", body="b",
                              slug="guide-teaser-live")
        g.publish(by=self.author)
        g.save()

        _begin_unpublished_revision(
            g, title="Guide DRAFT Title", intro="Guide DRAFT intro",
            slug="guide-teaser-draft", author=self.author,
        )

        item = to_teaser_item(g, "guide", language_code="en")
        self.assertEqual(item["title"], "Guide Live Title")
        self.assertEqual(item["teaser"], "Guide live intro")
        self.assertTrue(item["url"].endswith("/guides/guide-teaser-live/"))
        self.assertNotIn("guide-teaser-draft", item["url"])

    def test_live_url_returns_200(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="Guide URL Title", intro="i", body="b", slug="guide-teaser-url")
        g.publish(by=self.author)
        g.save()
        _begin_unpublished_revision(
            g, title="Guide URL Draft", intro="i2", slug="guide-teaser-url-draft", author=self.author,
        )
        item = to_teaser_item(g, "guide", language_code="en")
        resp = self.client.get(item["url"])
        self.assertEqual(resp.status_code, 200)


class PromptTeaserLiveRevisionTests(TeaserLiveRevisionTestsBase):
    def test_live_revision_shows_live_values_not_draft(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("en", title="Prompt Live Title", intro="Prompt live intro", body="b", outro="o",
                              slug="prompt-teaser-live")
        p.publish(by=self.author)
        p.save()

        _begin_unpublished_revision(
            p, title="Prompt DRAFT Title", intro="Prompt DRAFT intro",
            slug="prompt-teaser-draft", author=self.author,
        )

        item = to_teaser_item(p, "prompt", language_code="en")
        self.assertEqual(item["title"], "Prompt Live Title")
        self.assertEqual(item["teaser"], "Prompt live intro")
        self.assertTrue(item["url"].endswith("/prompts/prompt-teaser-live/"))
        self.assertNotIn("prompt-teaser-draft", item["url"])

    def test_live_url_returns_200(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("en", title="Prompt URL Title", intro="i", body="b", outro="o",
                              slug="prompt-teaser-url")
        p.publish(by=self.author)
        p.save()
        _begin_unpublished_revision(
            p, title="Prompt URL Draft", intro="i2", slug="prompt-teaser-url-draft", author=self.author,
        )
        item = to_teaser_item(p, "prompt", language_code="en")
        resp = self.client.get(item["url"])
        self.assertEqual(resp.status_code, 200)


def _edit_translation_without_republishing(obj, *, title, intro, slug):
    """
    UseCase/Comparison's visible_in_language() uses strict published()
    (a deliberate Beta 8.8/8.9a decision, unlike Guide/Prompt's broader
    visible_on_site()) - these two models have no "stays visible during
    review with a live revision" concept at all, so moving them out of
    `published` status would make the whole object 404, not just its new
    slug. The realistic draft-leak scenario for them is instead: status
    stays `published` throughout, but the translation is edited in place
    without the live snapshot ever being refreshed (no new publish() call).
    """
    obj.title = title
    obj.intro = intro
    obj.slug = slug
    obj.save()


class UseCaseTeaserLiveRevisionTests(TeaserLiveRevisionTestsBase):
    def test_edit_without_republish_shows_live_values_not_draft(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        u.create_translation("en", title="UC Live Title", intro="UC live intro", body="b", outro="o",
                              slug="uc-teaser-live", persona="Founder")
        u.publish(by=self.author)
        u.save()

        _edit_translation_without_republishing(
            u, title="UC DRAFT Title", intro="UC DRAFT intro", slug="uc-teaser-draft",
        )

        item = to_teaser_item(u, "usecase", language_code="en")
        self.assertEqual(item["title"], "UC Live Title")
        self.assertEqual(item["teaser"], "UC live intro")
        self.assertTrue(item["url"].endswith("/usecases/uc-teaser-live/"))
        self.assertNotIn("uc-teaser-draft", item["url"])

    def test_live_url_returns_200(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        u.create_translation("en", title="UC URL Title", intro="i", body="b", outro="o",
                              slug="uc-teaser-url", persona="Founder")
        u.publish(by=self.author)
        u.save()
        _edit_translation_without_republishing(
            u, title="UC URL Draft", intro="i2", slug="uc-teaser-url-draft",
        )
        item = to_teaser_item(u, "usecase", language_code="en")
        resp = self.client.get(item["url"])
        self.assertEqual(resp.status_code, 200)


class ComparisonTeaserLiveRevisionTests(TeaserLiveRevisionTestsBase):
    """Comparison has no display_title/display_intro/display_body
    properties (a separate, documented, unfixed model gap) - the teaser
    resolver reads its live_i18n snapshot directly instead. Like UseCase,
    Comparison's visible_in_language() is strict published()-only, so the
    realistic scenario is an in-place edit without republishing (see
    _edit_translation_without_republishing())."""

    def test_edit_without_republish_shows_live_values_not_draft(self):
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        c.create_translation("en", title="Cmp Live Title", intro="Cmp live intro", body="b",
                              slug="cmp-teaser-live")
        c.publish(by=self.author)
        c.save()

        _edit_translation_without_republishing(
            c, title="Cmp DRAFT Title", intro="Cmp DRAFT intro", slug="cmp-teaser-draft",
        )

        item = to_teaser_item(c, "comparison", language_code="en")
        self.assertEqual(item["title"], "Cmp Live Title")
        self.assertEqual(item["teaser"], "Cmp live intro")
        self.assertTrue(item["url"].endswith("/compare/cmp-teaser-live/"))
        self.assertNotIn("cmp-teaser-draft", item["url"])

    def test_live_url_returns_200(self):
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        c.create_translation("en", title="Cmp URL Title", intro="i", body="b", slug="cmp-teaser-url")
        c.publish(by=self.author)
        c.save()
        _edit_translation_without_republishing(
            c, title="Cmp URL Draft", intro="i2", slug="cmp-teaser-url-draft",
        )
        item = to_teaser_item(c, "comparison", language_code="en")
        resp = self.client.get(item["url"])
        self.assertEqual(resp.status_code, 200)

    def test_comparison_without_live_snapshot_falls_back_to_current_translation(self):
        # Documented behavior: a Comparison genuinely published without
        # ever going through publish() (no live_i18n at all, matching
        # existing Beta 8.9-era test fixtures) still gets a usable teaser
        # via the current-translation fallback.
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        c.create_translation("en", title="No Snapshot Title", intro="No snapshot intro", body="b",
                              slug="cmp-no-snapshot")
        item = to_teaser_item(c, "comparison", language_code="en")
        self.assertEqual(item["title"], "No Snapshot Title")
        self.assertEqual(item["teaser"], "No snapshot intro")


class TeaserLanguageStateTests(TestCase):
    """Section G: the teaser helper must not mutate ambient Django
    language or the object's own Parler current-language state, and must
    never surface a different language's content via Parler's own
    fallback."""

    def test_ambient_django_language_unchanged_after_call(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        g.create_translation("en", title="Lang State Guide", intro="i", body="b", slug="lang-state-guide")

        before = get_language()
        to_teaser_item(g, "guide", language_code="de")
        after = get_language()
        self.assertEqual(before, after)

    def test_parler_current_language_unchanged_after_call(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        g.create_translation("en", title="Lang State Guide 2", intro="i", body="b", slug="lang-state-guide-2")
        with switch_language(g, "en"):
            pass
        before = g.get_current_language()
        to_teaser_item(g, "guide", language_code="de")
        after = g.get_current_language()
        self.assertEqual(before, after)

    def test_no_wrong_language_fallback_in_title(self):
        # EN-only guide, teaser requested for "de" - must not silently
        # substitute the EN title via Parler's own PARLER_LANGUAGES fallback.
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        g.create_translation("en", title="EN Only Fallback Check", intro="i", body="b",
                              slug="en-only-fallback-check")
        item = to_teaser_item(g, "guide", language_code="de")
        self.assertNotEqual(item["title"], "EN Only Fallback Check")

    def test_missing_active_translation_yields_empty_title_not_foreign_language(self):
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        c.create_translation("de", title="DE Only Cmp", intro="i", body="b", slug="de-only-cmp-fallback-check")
        item = to_teaser_item(c, "comparison", language_code="en")
        self.assertNotEqual(item["title"], "DE Only Cmp")
