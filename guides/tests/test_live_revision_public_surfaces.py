"""
Beta 8.10a Sections B (extension)/D/E/H/J: every public surface that shows
a guide mid live-revision - the detail page's SEO/canonical/hreflang, the
Related Guides widget, the homepage "Aktuelle Inhalte" section, and the
starter CTA/footer link - must show exclusively the last published live
title/intro/URL, never the new, unpublished draft.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from catalog.models import Category
from core.models.editorial import EditorialWorkflowMixin
from core.services import get_latest_items, related_guides
from guides.models import Guide

User = get_user_model()


def _publish_guide(*, author, slug, title, intro, body, is_starter=False):
    g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=author, is_starter=is_starter)
    g.create_translation("en", title=title, intro=intro, body=body, slug=slug)
    g.publish(by=author)
    g.save()
    return g


def _begin_unpublished_revision(g, *, title, intro, slug, author):
    g.title = title
    g.intro = intro
    g.slug = slug
    g.save()
    g.move_to_review(by=author)
    g.last_published_revision_id = 1
    g.save()


class GuideDetailSeoLiveRevisionTests(TestCase):
    """Section B (extension): SEO title/canonical/hreflang on the guide's
    own detail page during a live revision."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="seo-live-editor", email="seo-live@example.com", password="testpass123"
        )

    def test_canonical_and_seo_title_use_live_slug_and_title(self):
        g = _publish_guide(author=self.author, slug="seo-live-slug", title="SEO Live Title",
                            intro="i", body="b")
        _begin_unpublished_revision(g, title="SEO DRAFT Title", intro="i2",
                                     slug="seo-draft-slug", author=self.author)

        resp = self.client.get("/en/guides/seo-live-slug/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("SEO Live Title", html)
        self.assertIn("/en/guides/seo-live-slug/", html)
        self.assertNotIn("SEO DRAFT Title", html)
        self.assertNotIn("seo-draft-slug", html)

    def test_hreflang_alternates_contain_no_draft_slug(self):
        g = _publish_guide(author=self.author, slug="hreflang-live-slug", title="Hreflang Live",
                            intro="i", body="b")
        _begin_unpublished_revision(g, title="Hreflang DRAFT", intro="i2",
                                     slug="hreflang-draft-slug", author=self.author)

        resp = self.client.get("/en/guides/hreflang-live-slug/")
        html = resp.content.decode()
        self.assertNotIn("hreflang-draft-slug", html)


class RelatedGuidesLiveRevisionTests(TestCase):
    """Section D: a related guide mid live-revision must only ever expose
    its live title/intro/URL in the widget."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="related-live-editor", email="related-live@example.com", password="testpass123"
        )
        cls.category = Category.objects.create()
        from parler.utils.context import switch_language
        with switch_language(cls.category, "en"):
            cls.category.name = "Related Live Category"
            cls.category.slug = "related-live-category"
            cls.category.save()

    def test_related_card_shows_live_title_intro_and_url(self):
        current = _publish_guide(author=self.author, slug="related-current", title="Related Current",
                                  intro="i", body="b")
        current.categories.add(self.category)

        recommended = _publish_guide(author=self.author, slug="related-live-target",
                                      title="Related Live Title", intro="Related live intro",
                                      body="b")
        recommended.categories.add(self.category)
        _begin_unpublished_revision(
            recommended, title="Related DRAFT Title", intro="Related DRAFT intro",
            slug="related-draft-target", author=self.author,
        )

        result = related_guides(current, limit=6, language_code="en")
        self.assertIn(recommended.pk, [g.pk for g in result])

        resp = self.client.get("/en/guides/related-current/")
        self.assertEqual(resp.status_code, 200)
        related_items = resp.context["related_guides"]
        match = next(item for item in related_items if item["url"].endswith("related-live-target/"))
        self.assertEqual(match["title"], "Related Live Title")
        self.assertIn("Related live intro", match["teaser"])

        html = resp.content.decode()
        self.assertNotIn("Related DRAFT Title", html)
        self.assertNotIn("related-draft-target", html)

        link_check = self.client.get(match["url"])
        self.assertEqual(link_check.status_code, 200)

    def test_current_guide_excluded_ranking_and_limit_unchanged(self):
        current = _publish_guide(author=self.author, slug="related-exclude-current",
                                  title="Exclude Current", intro="i", body="b")
        current.categories.add(self.category)
        for i in range(10):
            g = _publish_guide(author=self.author, slug=f"related-limit-{i}",
                                title=f"Related Limit {i}", intro="i", body="b")
            g.categories.add(self.category)

        result = related_guides(current, limit=3, language_code="en")
        self.assertLessEqual(len(result), 3)
        self.assertNotIn(current.pk, [g.pk for g in result])


class LatestContentGuideLiveRevisionTests(TestCase):
    """Section E: the homepage "Aktuelle Inhalte" widget must show only
    the live title/intro/URL for a guide mid live-revision."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="latest-live-editor", email="latest-live@example.com", password="testpass123"
        )

    def test_latest_items_show_live_values_not_draft(self):
        g = _publish_guide(author=self.author, slug="latest-live-slug", title="Latest Live Title",
                            intro="Latest live intro", body="b")
        _begin_unpublished_revision(g, title="Latest DRAFT Title", intro="Latest DRAFT intro",
                                     slug="latest-draft-slug", author=self.author)

        items = get_latest_items(limit=20, language_code="en")
        match = next((i for i in items if i["badge"] == "Guide" and "latest-live-slug" in i["url"]), None)
        self.assertIsNotNone(match, "live guide missing from get_latest_items()")
        self.assertEqual(match["title"], "Latest Live Title")
        self.assertIn("Latest live intro", match["teaser"])
        self.assertNotIn("latest-draft-slug", match["url"])

        resp = self.client.get(match["url"])
        self.assertEqual(resp.status_code, 200)

    def test_homepage_html_has_no_draft_leak(self):
        g = _publish_guide(author=self.author, slug="home-live-slug", title="Home Live Title",
                            intro="Home live intro", body="b")
        _begin_unpublished_revision(g, title="Home DRAFT Title", intro="Home DRAFT intro",
                                     slug="home-draft-slug", author=self.author)

        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Home Live Title", html)
        self.assertNotIn("Home DRAFT Title", html)
        self.assertNotIn("home-draft-slug", html)


class StarterGuideLiveRevisionTests(TestCase):
    """Section H: a starter guide mid live-revision must keep pointing
    the CTA/footer/list at its live slug and title, never the draft."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="starter-live-editor", email="starter-live@example.com", password="testpass123"
        )

    def test_starter_cta_and_footer_use_live_slug_during_revision(self):
        starter = _publish_guide(author=self.author, slug="starter-live-slug", title="Starter Live Title",
                                  intro="i", body="b", is_starter=True)
        _begin_unpublished_revision(starter, title="Starter DRAFT Title", intro="i2",
                                     slug="starter-draft-slug", author=self.author)

        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("/en/guides/starter-live-slug/", html)
        self.assertNotIn("starter-draft-slug", html)
        self.assertNotIn("Starter DRAFT Title", html)

    def test_starter_remains_first_in_list_during_revision(self):
        starter = _publish_guide(author=self.author, slug="starter-list-live", title="Starter List Live",
                                  intro="i", body="b", is_starter=True)
        _begin_unpublished_revision(starter, title="Starter List DRAFT", intro="i2",
                                     slug="starter-list-draft", author=self.author)
        _publish_guide(author=self.author, slug="starter-list-other", title="Other Guide",
                        intro="i", body="b")

        resp = self.client.get("/en/guides/")
        objs = list(resp.context["object_list"])
        self.assertEqual(objs[0].pk, starter.pk)
