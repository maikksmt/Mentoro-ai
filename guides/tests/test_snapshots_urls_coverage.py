"""
Coverage-Schritt 3/3a: Guide.get_absolute_url() multi-language fallback,
Guide.on_after_publish()'s GuideSection live-snapshot sync, and the
Coverage-Schritt-3a snapshot-language-isolation contract - the parts of
guides/models.py not already exercised by test_live_revisions.py,
test_draft_slug_leak.py and test_language_safety.py (which cover the
snapshot-vs-draft priority and cross-language-slug-leak contracts already).

Coverage-Schritt 3a fix: EditorialWorkflowMixin.get_live_value() (core/models/
editorial.py), used by Guide.get_display_value()/display_intro/display_body,
used to fall back to *any other language's* snapshot value whenever the
requested language's own value was falsy (empty string), not only when it
was absent - a confirmed cross-language leak for blank=True fields (intro/
body). Fixed via the shared, pure core.models.editorial.get_snapshot_field()
helper: a non-empty snapshot is now authoritative for the requested language
alone, regardless of whether the field within it is empty, missing, or None.
GuideSnapshotLanguageIsolationTests below asserts the fixed, real contract as
a normal green regression test (formerly an expectedFailure).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide, GuideSection

User = get_user_model()


class GuideAbsoluteUrlFallbackTests(TestCase):
    def test_guide_with_no_translations_at_all_returns_hash(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        self.assertEqual(g.get_absolute_url(language="en"), "#")

    def test_draft_guide_uses_its_own_current_translation_slug(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        g.create_translation("en", title="T", intro="i", body="b", slug="draft-only-en")
        self.assertEqual(
            g.get_absolute_url(language="en"),
            reverse("guides:detail", kwargs={"slug": "draft-only-en"}),
        )

    def test_no_slug_in_requested_language_falls_back_to_another_available_languages_slug(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        g.create_translation("de", title="T", intro="i", body="b", slug="de-only-fallback")
        with translation.override("en"):
            url = g.get_absolute_url(language="en")
        # No "en" translation exists at all, so the loop over
        # get_available_languages() falls back to the only translation that
        # does exist ("de") - the URL prefix reflects whatever language is
        # ambient at reverse()-call time (the fallback loop does not itself
        # scope reverse() to the found slug's own language), which is the
        # real, current contract.
        self.assertEqual(url, "/en/guides/de-only-fallback/")


class GuideOnAfterPublishSectionSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="publish-sections", password="pass")

    def test_publish_syncs_live_snapshot_for_existing_sections(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="T", intro="i", body="b", slug="pub-with-sections")
        section = GuideSection.objects.create(guide=g, order=1)
        section.create_translation("en", title="Sec Title", body="Sec Body")

        g.publish(by=self.author)
        g.save()

        section = GuideSection.objects.get(pk=section.pk)
        self.assertEqual(section.live_i18n["en"]["title"], "Sec Title")
        self.assertEqual(section.live_i18n["en"]["body"], "Sec Body")

    def test_publish_with_no_sections_does_not_crash(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="T", intro="i", body="b", slug="pub-no-sections")
        g.publish(by=self.author)
        g.save()
        g = Guide.objects.get(pk=g.pk)
        self.assertEqual(g.status, EditorialWorkflowMixin.STATUS_PUBLISHED)

class GuideSnapshotLanguageIsolationTests(TestCase):
    """The Coverage-Schritt-3a snapshot contract for Guide, exercised through
    the public display_intro/display_body properties (not the private
    get_live_value() helper directly)."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="snapshot-author", password="pass")

    def test_empty_field_in_requested_language_snapshot_stays_empty_not_german(self):
        # Fall A: requested language present in a non-empty snapshot, field
        # itself empty - authoritative, never falls back to DE.
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="T", intro="", body="b", slug="leak-en")
        g.create_translation("de", title="T", intro="Deutsche Einleitung", body="b", slug="leak-de")
        g.publish(by=self.author)
        g.save()
        g = Guide.objects.get(pk=g.pk)

        with translation.override("en"):
            self.assertEqual(g.display_intro, "")

    def test_missing_key_in_requested_language_snapshot_stays_empty(self):
        # Fall A variant: the EN snapshot entry exists but never got an
        # "intro" key at all (not even an empty string) - still authoritative.
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="T", intro="something", body="b", slug="missing-key-en")
        g.publish(by=self.author)
        g.save()
        g = Guide.objects.get(pk=g.pk)
        snapshot = dict(g.live_i18n)
        del snapshot["en"]["intro"]
        Guide.objects.filter(pk=g.pk).update(live_i18n=snapshot)
        g = Guide.objects.get(pk=g.pk)

        with translation.override("en"):
            self.assertEqual(g.display_intro, "")

    def test_requested_language_missing_from_nonempty_snapshot_stays_empty_no_draft_leak(self):
        # Fall B: snapshot is non-empty but has no "en" entry at all (e.g.
        # published in German only) - "" is returned, and a *later* EN draft
        # translation must not leak through either.
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("de", title="T", intro="Deutscher Text", body="b", slug="de-only-pub")
        g.publish(by=self.author)
        g.save()
        g = Guide.objects.get(pk=g.pk)
        self.assertNotIn("en", g.live_i18n)

        # A draft EN translation is added afterwards - must still not surface.
        g.create_translation("en", title="Draft EN", intro="Draft EN intro", body="b", slug="de-only-pub-en-draft")
        g.save()
        g = Guide.objects.get(pk=g.pk)

        with translation.override("en"):
            self.assertEqual(g.display_intro, "")

    def test_legacy_missing_snapshot_uses_same_language_draft_only(self):
        # Fall C: no snapshot at all (never published) - same-language draft
        # translation may be used, but a DE-only translation must not leak
        # into an EN request.
        g_en = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        g_en.create_translation("en", title="T", intro="Draft intro", body="b", slug="legacy-en-only")
        self.assertEqual(g_en.live_i18n, {})
        with translation.override("en"):
            self.assertEqual(g_en.display_intro, "Draft intro")

        g_de = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        g_de.create_translation("de", title="T", intro="Deutscher Draft", body="b", slug="legacy-de-only")
        self.assertEqual(g_de.live_i18n, {})
        with translation.override("en"):
            # No "en" translation exists at all (has_translation("en") is
            # False) - _current_values_for() returns {} rather than
            # substituting Parler's own fallback language, so
            # get_display_value() naturally yields None (its established
            # "nothing available" contract - see also
            # test_no_translation_and_no_snapshot_returns_none in
            # test_guide_section_model.py), never the German text.
            self.assertIsNone(g_de.display_intro)

    def test_published_snapshot_stays_authoritative_over_later_draft_edits(self):
        # A later EN draft edit must not replace the published EN snapshot,
        # and a later DE draft edit must never leak into the EN value.
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="T", intro="Published intro", body="b", slug="stable-en")
        g.publish(by=self.author)
        g.save()
        g = Guide.objects.get(pk=g.pk)

        g.set_current_language("en")
        g.intro = "New unpublished EN draft"
        g.save()
        g.create_translation("de", title="T", intro="Neuer DE Draft", body="b", slug="stable-de")
        g = Guide.objects.get(pk=g.pk)

        with translation.override("en"):
            self.assertEqual(g.display_intro, "Published intro")
