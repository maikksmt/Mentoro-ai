"""
Beta 11.7 groups B/D: draft-vs-live values and the slug contract.

Now that a use case stays public through a review round, its saved draft
slug and draft text sit in the database right next to the published
snapshot. Everything public must keep reading the snapshot.
"""
from django.conf import settings
from django.test import TestCase
from django.utils import translation

from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)

LIVE_TITLE = "Live Title A"
LIVE_INTRO = "Live intro A"
LIVE_BODY = "Live body A"
LIVE_OUTRO = "Live outro A"
LIVE_SLUG = "live-slug-a"

DRAFT_TITLE = "Draft Title B"
DRAFT_INTRO = "Draft intro B"
DRAFT_BODY = "Draft body B"
DRAFT_OUTRO = "Draft outro B"
DRAFT_SLUG = "draft-slug-b"


class DraftVersusLiveOnTheDetailPageTests(TestCase):
    """Group B: only the last published values are ever rendered."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-dvl-author")
        self.usecase = make_usecase(
            slug=LIVE_SLUG, title=LIVE_TITLE, intro=LIVE_INTRO,
            body=LIVE_BODY, outro=LIVE_OUTRO, author=self.author,
        )
        publish(self.usecase, self.author)
        save_draft_edit(
            self.usecase, "en",
            title=DRAFT_TITLE, intro=DRAFT_INTRO, body=DRAFT_BODY,
            outro=DRAFT_OUTRO, slug=DRAFT_SLUG,
        )
        start_review_round(self.usecase, self.author)

    def _live_page(self):
        return self.client.get(f"/en/usecases/{LIVE_SLUG}/")

    def test_live_slug_still_resolves_during_review(self):
        self.assertEqual(self._live_page().status_code, 200)

    def test_draft_slug_does_not_resolve_before_republish(self):
        self.assertEqual(self.client.get(f"/en/usecases/{DRAFT_SLUG}/").status_code, 404)

    def test_detail_page_renders_live_title_intro_body_outro(self):
        html = self._live_page().content.decode()
        for value in (LIVE_TITLE, LIVE_INTRO, LIVE_BODY, LIVE_OUTRO):
            with self.subTest(value=value):
                self.assertIn(value, html)

    def test_detail_page_renders_no_draft_value_at_all(self):
        html = self._live_page().content.decode()
        for value in (DRAFT_TITLE, DRAFT_INTRO, DRAFT_BODY, DRAFT_OUTRO, DRAFT_SLUG):
            with self.subTest(value=value):
                self.assertNotIn(value, html)

    def test_seo_title_and_description_come_from_the_snapshot(self):
        seo = self._live_page().context["seo"]
        self.assertEqual(seo.title, LIVE_TITLE)
        self.assertIn("Live intro", seo.description)
        self.assertNotIn("Draft", seo.description)

    def test_canonical_uses_the_live_slug(self):
        canonical = self._live_page().context["seo"].canonical
        self.assertIn(LIVE_SLUG, canonical)
        self.assertNotIn(DRAFT_SLUG, canonical)

    def test_json_ld_uses_live_values_only(self):
        json_ld = self._live_page().context["seo"].json_ld
        self.assertEqual(json_ld["name"], LIVE_TITLE)
        self.assertIn(LIVE_SLUG, json_ld["url"])
        self.assertNotIn(DRAFT_SLUG, json_ld["url"])

    def test_breadcrumb_uses_the_live_title(self):
        crumbs = self._live_page().context["crumbs"]
        labels = [str(label) for label, _url in crumbs]
        self.assertIn(LIVE_TITLE, labels)
        self.assertNotIn(DRAFT_TITLE, labels)

    def test_hreflang_alternates_never_carry_the_draft_slug(self):
        for alt in self._live_page().context["seo"].alternates:
            with self.subTest(lang=alt.lang):
                self.assertNotIn(DRAFT_SLUG, alt.url)

    def test_get_absolute_url_returns_the_live_slug(self):
        usecase = UseCase.objects.get(pk=self.usecase.pk)
        self.assertIn(LIVE_SLUG, usecase.get_absolute_url(language="en"))
        self.assertNotIn(DRAFT_SLUG, usecase.get_absolute_url(language="en"))


class RepublishActivatesTheNewValuesTests(TestCase):
    """Group B, second half: publishing again promotes the draft to live."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-republish-author")
        self.usecase = make_usecase(
            slug="republish-live", title=LIVE_TITLE, intro=LIVE_INTRO,
            body=LIVE_BODY, outro=LIVE_OUTRO, author=self.author,
        )
        publish(self.usecase, self.author)
        save_draft_edit(
            self.usecase, "en",
            title=DRAFT_TITLE, intro=DRAFT_INTRO, body=DRAFT_BODY,
            outro=DRAFT_OUTRO, slug="republish-draft",
        )
        in_review = start_review_round(self.usecase, self.author)
        publish(in_review, self.author)

    def test_new_slug_resolves_after_republish(self):
        resp = self.client.get("/en/usecases/republish-draft/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(DRAFT_TITLE, resp.content.decode())

    def test_new_values_are_rendered_after_republish(self):
        html = self.client.get("/en/usecases/republish-draft/").content.decode()
        for value in (DRAFT_TITLE, DRAFT_INTRO, DRAFT_BODY, DRAFT_OUTRO):
            with self.subTest(value=value):
                self.assertIn(value, html)

    def test_public_slug_stays_the_stable_permalink_after_republish(self):
        """Existing (unchanged) permalink semantics: ``public_slug`` is the
        stable public address. ``publish()`` writes the live snapshot before
        ``on_after_publish()`` syncs public_slug to the new slug, so the
        snapshot keeps the original public_slug and canonical stays put -
        the same order Guide and Prompt use. Beta 11.7 deliberately does not
        touch this; it only had to guarantee the draft slug is unreachable
        *before* republishing, which the previous test class covers."""
        usecase = UseCase.objects.get(pk=self.usecase.pk)
        self.assertIn("republish-live", usecase.get_absolute_url(language="en"))

    def test_both_the_permalink_and_the_new_slug_resolve_after_republish(self):
        for slug in ("republish-live", "republish-draft"):
            with self.subTest(slug=slug):
                resp = self.client.get(f"/en/usecases/{slug}/")
                self.assertEqual(resp.status_code, 200)
                self.assertIn(DRAFT_TITLE, resp.content.decode())


class HistoricalRecordsKeepResolvingTests(TestCase):
    """A published record predating the snapshot mechanism must not break."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def test_published_usecase_without_a_snapshot_still_resolves(self):
        from core.models.editorial import EditorialWorkflowMixin
        from django.utils import timezone

        usecase = UseCase.objects.create(
            status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
        )
        usecase.create_translation(
            "en", title="Historical", intro="i", body="b", outro="o",
            slug="historical-no-snapshot", persona="",
        )
        self.assertEqual(usecase.live_i18n, {})
        self.assertEqual(self.client.get("/en/usecases/historical-no-snapshot/").status_code, 200)

    def test_review_status_without_a_snapshot_does_not_expose_its_draft_slug(self):
        """The compat branch in _resolve_by_slug() stays pinned to
        STATUS_PUBLISHED, so widening the queryset cannot make a snapshot-less
        review object resolvable through its current translation slug."""
        from core.models.editorial import EditorialWorkflowMixin

        usecase = UseCase.objects.create(
            status=EditorialWorkflowMixin.STATUS_REVIEW, last_published_revision_id=1
        )
        usecase.create_translation(
            "en", title="No Snapshot Review", intro="i", body="b", outro="o",
            slug="review-no-snapshot", persona="",
        )
        self.assertEqual(usecase.live_i18n, {})
        self.assertEqual(self.client.get("/en/usecases/review-no-snapshot/").status_code, 404)
