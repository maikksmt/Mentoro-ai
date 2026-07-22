"""
Beta 11.5 groups F/G: cache isolation and data integrity.

The one cache this project keeps for public pages is
``core.services.get_public_inventory`` (``mentoroai:public-inventory:<v>:<lang>``,
footer/homepage figures). There is no prompt-detail view cache, no
related-content cache and no template fragment cache, so the preview's job is
simply to read public data only, write nothing, and stay uncacheable itself.

The preview is also strictly read-only: a GET must not save, transition the
FSM, write a revision or touch the published snapshot.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Version

from prompts.models import Prompt
from prompts.tests.draft_preview_fixtures import (
    make_draft_prompt,
    make_user,
    publish,
    save_translation_edit,
)

DRAFT_MARKER = "Draft Only Marker Text"
LIVE_MARKER = "Live Snapshot Marker Text"


def preview_url(prompt_pk, language_code="en"):
    return reverse("admin:prompts_prompt_draft_preview", args=[prompt_pk, language_code])


class PreviewCacheIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.editor = make_user("cache-editor", group="Editor")
        self.client.force_login(self.editor)

        self.prompt = make_draft_prompt(
            self.editor,
            slug="cache-prompt-en",
            title=LIVE_MARKER,
            intro="live intro",
            body=f"<p>{LIVE_MARKER}</p>",
        )
        publish(self.prompt, self.editor)
        save_translation_edit(
            self.prompt,
            "en",
            title=DRAFT_MARKER,
            intro="draft intro",
            body=f"<p>{DRAFT_MARKER}</p>",
        )

    def _public(self):
        return self.client.get("/en/prompts/cache-prompt-en/")

    def _preview(self):
        return self.client.get(preview_url(self.prompt.pk))

    def test_preview_then_public_keeps_public_on_the_snapshot(self):
        preview = self._preview()
        self.assertContains(preview, DRAFT_MARKER)

        public = self._public()
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, LIVE_MARKER)
        self.assertNotContains(public, DRAFT_MARKER)

    def test_public_then_preview_still_shows_the_draft(self):
        public = self._public()
        self.assertContains(public, LIVE_MARKER)
        self.assertNotContains(public, DRAFT_MARKER)

        preview = self._preview()
        self.assertContains(preview, DRAFT_MARKER)

    def test_repeated_previews_never_leak_into_public(self):
        for _ in range(3):
            self.assertContains(self._preview(), DRAFT_MARKER)
        public = self._public()
        self.assertContains(public, LIVE_MARKER)
        self.assertNotContains(public, DRAFT_MARKER)

    def test_preview_writes_no_draft_value_into_the_public_inventory_cache(self):
        self._preview()
        for language in ("en", "de"):
            key = f"mentoroai:public-inventory:v5:{language}"
            with self.subTest(language=language):
                self.assertNotIn(DRAFT_MARKER, repr(cache.get(key)))

    def test_preview_response_is_not_publicly_cacheable(self):
        resp = self._preview()
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertIn("private", resp["Cache-Control"])

    def test_public_response_contract_is_untouched_by_the_preview(self):
        self._preview()
        public = self._public()
        self.assertNotIn("no-store", public.get("Cache-Control", ""))


class PreviewDataIntegrityTests(TestCase):
    """Group G: a preview GET changes nothing at all."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("integrity-editor", group="Editor")
        self.client.force_login(self.editor)

        self.prompt = make_draft_prompt(
            self.editor, slug="integrity-en", title="Integrity Prompt"
        )
        publish(self.prompt, self.editor)
        save_translation_edit(self.prompt, "en", title="Integrity Draft Title")

    def _snapshot_state(self):
        prompt = Prompt.objects.get(pk=self.prompt.pk)
        return {
            "status": prompt.status,
            "live_i18n": prompt.live_i18n,
            "submitted_for_review_at": prompt.submitted_for_review_at,
            "reviewed_at": prompt.reviewed_at,
            "reviewed_by_id": prompt.reviewed_by_id,
            "published_at": prompt.published_at,
            "updated_at": prompt.updated_at,
            "last_published_revision_id": prompt.last_published_revision_id,
            "title": prompt.safe_translation_getter("title", language_code="en"),
            "tools": list(prompt.tools.values_list("pk", flat=True)),
            "tags": list(prompt.tags.names()),
            "revision_count": Version.objects.get_for_object(prompt).count(),
        }

    def test_preview_get_changes_no_persisted_state(self):
        before = self._snapshot_state()
        resp = self.client.get(preview_url(self.prompt.pk))
        self.assertEqual(resp.status_code, 200)
        after = self._snapshot_state()
        self.assertEqual(before, after)

    def test_repeated_previews_change_no_persisted_state(self):
        before = self._snapshot_state()
        for _ in range(3):
            self.client.get(preview_url(self.prompt.pk))
        self.assertEqual(before, self._snapshot_state())

    def test_preview_creates_no_revision(self):
        before = Version.objects.count()
        self.client.get(preview_url(self.prompt.pk))
        self.assertEqual(Version.objects.count(), before)
