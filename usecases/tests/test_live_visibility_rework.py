"""
Beta 11.7A: a published use case stays public while its new draft is being
reworked.

``published -> review -> rework`` is a real, reachable path (the admin's
auto-review guard produces the first step, an editor's "request changes" the
second). Rework means the *new* draft needs another pass, not that the
published state was withdrawn - so the live snapshot must keep serving.
Archiving remains the deliberate withdrawal.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import translation

from core.models.editorial import EditorialWorkflowMixin
from core.services import related_usecases, to_teaser_item
from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    add_translation,
    archive,
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)

LIVE_TITLE = "Rework Live Title A"
LIVE_PERSONA = "Rework Live Persona A"
LIVE_SLUG = "rework-live-a"
DRAFT_TITLE = "Rework Draft Title B"
DRAFT_PERSONA = "Rework Draft Persona B"
DRAFT_SLUG = "rework-draft-b"


def request_rework(usecase, by):
    """Editor sends the new draft back: review -> rework."""
    fresh = UseCase.objects.get(pk=usecase.pk)
    fresh.request_rework(by=by, note="")
    fresh.save()
    return UseCase.objects.get(pk=fresh.pk)


class ReworkKeepsThePublishedStateOnlineTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("uc-rework-author")
        self.editor = make_user("uc-rework-editor")

        self.usecase = make_usecase(
            slug=LIVE_SLUG, title=LIVE_TITLE, intro="Rework live intro A",
            body="Rework live body A", outro="Rework live outro A",
            persona=LIVE_PERSONA, author=self.author,
        )
        publish(self.usecase, self.author)
        save_draft_edit(
            self.usecase, "en",
            title=DRAFT_TITLE, intro="Rework draft intro B",
            body="Rework draft body B", outro="Rework draft outro B",
            slug=DRAFT_SLUG, persona=DRAFT_PERSONA,
        )
        start_review_round(self.usecase, self.author)
        self.reworked = request_rework(self.usecase, self.editor)

    def test_status_really_is_rework(self):
        self.assertEqual(self.reworked.status, EditorialWorkflowMixin.STATUS_REWORK)

    def test_still_in_the_public_queryset(self):
        self.assertTrue(
            UseCase.objects.visible_in_language("en").filter(pk=self.usecase.pk).exists()
        )

    def test_live_slug_returns_200_and_shows_the_published_values(self):
        resp = self.client.get(f"/en/usecases/{LIVE_SLUG}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(LIVE_TITLE, html)
        self.assertIn("Rework live body A", html)
        self.assertNotIn(DRAFT_TITLE, html)
        self.assertNotIn("Rework draft body B", html)

    def test_draft_slug_still_404s(self):
        self.assertEqual(self.client.get(f"/en/usecases/{DRAFT_SLUG}/").status_code, 404)

    def test_list_page_shows_live_title_and_live_persona(self):
        html = self.client.get("/en/usecases/").content.decode()
        self.assertIn(LIVE_TITLE, html)
        self.assertIn(LIVE_PERSONA, html)
        self.assertNotIn(DRAFT_TITLE, html)
        self.assertNotIn(DRAFT_PERSONA, html)

    def test_canonical_uses_the_live_slug(self):
        canonical = self.client.get(f"/en/usecases/{LIVE_SLUG}/").context["seo"].canonical
        self.assertIn(LIVE_SLUG, canonical)
        self.assertNotIn(DRAFT_SLUG, canonical)

    def test_sitemap_advertises_the_live_slug_only(self):
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertIn(LIVE_SLUG, xml)
        self.assertNotIn(DRAFT_SLUG, xml)

    def test_teaser_and_related_content_use_live_values(self):
        other = make_usecase(slug="rework-neighbour", title="Neighbour Live", author=self.author)
        publish(other, self.author)

        related = related_usecases(UseCase.objects.get(pk=other.pk), limit=6, language_code="en")
        items = [to_teaser_item(u, "usecase", language_code="en") for u in related]
        titles = [i["title"] for i in items]
        self.assertIn(LIVE_TITLE, titles)
        self.assertNotIn(DRAFT_TITLE, titles)
        for item in items:
            with self.subTest(url=item["url"]):
                self.assertNotIn(DRAFT_SLUG, item["url"])

    def test_republishing_after_rework_activates_the_new_values(self):
        republished = publish(self.reworked, self.editor)
        self.assertEqual(republished.status, EditorialWorkflowMixin.STATUS_PUBLISHED)

        resp = self.client.get(f"/en/usecases/{DRAFT_SLUG}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(DRAFT_TITLE, html)

        listing = self.client.get("/en/usecases/").content.decode()
        self.assertIn(DRAFT_PERSONA, listing)


class ReworkWithoutAPublishedStateStaysHiddenTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-rework-hidden-author")
        self.editor = make_user("uc-rework-hidden-editor")

    def test_never_published_use_case_in_rework_is_invisible(self):
        usecase = make_usecase(
            slug="rework-never-published", title="Rework Never Published",
            author=self.author,
        )
        usecase.move_to_review(by=self.author)
        usecase.save()
        reworked = request_rework(usecase, self.editor)

        self.assertEqual(reworked.status, EditorialWorkflowMixin.STATUS_REWORK)
        self.assertFalse(
            UseCase.objects.visible_in_language("en").filter(pk=usecase.pk).exists()
        )
        self.assertEqual(
            self.client.get("/en/usecases/rework-never-published/").status_code, 404
        )

    def test_archived_use_case_with_a_live_snapshot_stays_invisible(self):
        usecase = make_usecase(
            slug="rework-archived", title="Rework Archived", author=self.author
        )
        publish(usecase, self.author)
        archived = archive(usecase, self.editor)

        self.assertEqual(archived.status, EditorialWorkflowMixin.STATUS_ARCHIVED)
        self.assertTrue(archived.live_i18n)
        self.assertIsNotNone(archived.last_published_revision_id)
        self.assertFalse(
            UseCase.objects.visible_in_language("en").filter(pk=usecase.pk).exists()
        )
        self.assertEqual(self.client.get("/en/usecases/rework-archived/").status_code, 404)


class ReworkLanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-rework-lang-author")
        self.editor = make_user("uc-rework-lang-editor")

    def test_rework_keeps_both_published_languages_on_their_own_snapshot(self):
        usecase = make_usecase(
            slug="rework-bi-en", title="Rework English Live",
            persona="EN Live Persona", author=self.author,
        )
        add_translation(
            usecase, "de", slug="rework-bi-de", title="Rework Deutsch Live",
            persona="DE Live Persona",
        )
        publish(usecase, self.author)
        save_draft_edit(usecase, "en", title="Rework EN Draft")
        save_draft_edit(usecase, "de", title="Rework DE Entwurf")
        start_review_round(usecase, self.author)
        request_rework(usecase, self.editor)

        english = self.client.get("/en/usecases/rework-bi-en/")
        german = self.client.get("/de/usecases/rework-bi-de/")
        self.assertEqual(english.status_code, 200)
        self.assertEqual(german.status_code, 200)
        self.assertIn("Rework English Live", english.content.decode())
        self.assertNotIn("Rework EN Draft", english.content.decode())
        self.assertIn("Rework Deutsch Live", german.content.decode())
        self.assertNotIn("Rework DE Entwurf", german.content.decode())

    def test_rework_does_not_expose_a_language_without_its_own_snapshot(self):
        usecase = make_usecase(
            slug="rework-en-only", title="Rework EN Only", author=self.author
        )
        publish(usecase, self.author)
        add_translation(usecase, "de", slug="rework-de-draft", title="Rework DE Entwurf")
        start_review_round(usecase, self.author)
        request_rework(usecase, self.editor)

        self.assertEqual(self.client.get("/en/usecases/rework-en-only/").status_code, 200)
        self.assertEqual(self.client.get("/de/usecases/rework-de-draft/").status_code, 404)
        self.assertFalse(
            UseCase.objects.visible_in_language("de").filter(pk=usecase.pk).exists()
        )
