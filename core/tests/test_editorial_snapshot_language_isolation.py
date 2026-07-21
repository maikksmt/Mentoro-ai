"""
Coverage-Schritt 3a: the published-snapshot language-isolation contract of
EditorialWorkflowMixin.get_live_value() (core/models/editorial.py), verified
across every real subclass that uses it - Guide (see also
guides/tests/test_snapshots_urls_coverage.py and test_guide_section_model.py
for Guide's/GuideSection's full Pflichtfälle set), and one representative
test per remaining content type here: Prompt, UseCase, and Comparison.

Bug (fixed in this slice): get_live_value() used to fall back to *any other
language's* snapshot value whenever the requested language's own value was
falsy, not only when absent - a cross-language leak for blank=True fields
(intro/body/outro). Fixed via the shared, pure get_snapshot_field() helper:
a non-empty snapshot is authoritative for the requested language alone,
regardless of whether the field within it is empty, missing, or None; only a
completely empty/missing snapshot allows a same-language legacy draft
fallback (also hardened here: _current_values_for() on Guide/Prompt/UseCase
used to silently substitute Parler's own PARLER_LANGUAGES fallback language
for a missing translation - now guarded with has_translation(lang) first).

Comparison has no display_intro/get_display_value convenience (it manages
its own get_absolute_url() slug resolution independently, already correct
since Beta 8.11 - see compare/models.py) - but it still inherits
get_live_value() from EditorialWorkflowMixin directly, and compare/views.py's
ComparisonDetailView.get_context_data() calls it directly for title/intro/
body, so testing get_live_value() itself (not a private helper - a public
method every EditorialWorkflowMixin subclass exposes) is the real, reachable
contract for Comparison.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import translation

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()


class PromptSnapshotLanguageIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="prompt-snapshot-author", password="pass")

    def test_empty_field_in_requested_language_snapshot_stays_empty(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("en", title="T", intro="", body="b", outro="o", slug="prompt-leak-en")
        p.create_translation("de", title="T", intro="Deutsche Einleitung", body="b", outro="o", slug="prompt-leak-de")
        p.publish(by=self.author)
        p.save()
        p = Prompt.objects.get(pk=p.pk)

        with translation.override("en"):
            self.assertEqual(p.display_intro, "")

    def test_requested_language_missing_from_nonempty_snapshot_stays_empty_no_draft_leak(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("de", title="T", intro="Deutscher Text", body="b", outro="o", slug="prompt-de-only")
        p.publish(by=self.author)
        p.save()
        p = Prompt.objects.get(pk=p.pk)
        self.assertNotIn("en", p.live_i18n)

        p.create_translation("en", title="Draft EN", intro="Draft EN intro", body="b", outro="o",
                              slug="prompt-de-only-en-draft")
        p.save()
        p = Prompt.objects.get(pk=p.pk)

        with translation.override("en"):
            self.assertEqual(p.display_intro, "")

    def test_legacy_missing_snapshot_uses_same_language_draft_only(self):
        p_en = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        p_en.create_translation("en", title="T", intro="Draft intro", body="b", outro="o", slug="prompt-legacy-en")
        self.assertEqual(p_en.live_i18n, {})
        with translation.override("en"):
            self.assertEqual(p_en.display_intro, "Draft intro")

        p_de = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        p_de.create_translation("de", title="T", intro="Deutscher Draft", body="b", outro="o",
                                 slug="prompt-legacy-de")
        self.assertEqual(p_de.live_i18n, {})
        with translation.override("en"):
            # No "en" translation exists - _current_values_for() returns {}
            # rather than substituting Parler's own fallback language, so
            # get_display_value() yields None, never the German text.
            self.assertIsNone(p_de.display_intro)


class UseCaseSnapshotLanguageIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="usecase-snapshot-author", password="pass")

    def test_empty_field_in_requested_language_snapshot_stays_empty(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        u.create_translation("en", title="T", intro="", body="b", outro="o", persona="p", slug="uc-leak-en")
        u.create_translation("de", title="T", intro="Deutsche Einleitung", body="b", outro="o", persona="p",
                              slug="uc-leak-de")
        u.publish(by=self.author)
        u.save()
        u = UseCase.objects.get(pk=u.pk)

        with translation.override("en"):
            self.assertEqual(u.display_intro, "")

    def test_requested_language_missing_from_nonempty_snapshot_stays_empty_no_draft_leak(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        u.create_translation("de", title="T", intro="Deutscher Text", body="b", outro="o", persona="p",
                              slug="uc-de-only")
        u.publish(by=self.author)
        u.save()
        u = UseCase.objects.get(pk=u.pk)
        self.assertNotIn("en", u.live_i18n)

        u.create_translation("en", title="Draft EN", intro="Draft EN intro", body="b", outro="o", persona="p",
                              slug="uc-de-only-en-draft")
        u.save()
        u = UseCase.objects.get(pk=u.pk)

        with translation.override("en"):
            self.assertEqual(u.display_intro, "")

    def test_legacy_missing_snapshot_uses_same_language_draft_only(self):
        u_en = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        u_en.create_translation("en", title="T", intro="Draft intro", body="b", outro="o", persona="p",
                                 slug="uc-legacy-en")
        self.assertEqual(u_en.live_i18n, {})
        with translation.override("en"):
            self.assertEqual(u_en.display_intro, "Draft intro")

        u_de = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        u_de.create_translation("de", title="T", intro="Deutscher Draft", body="b", outro="o", persona="p",
                                 slug="uc-legacy-de")
        self.assertEqual(u_de.live_i18n, {})
        with translation.override("en"):
            # No "en" translation exists - _current_values_for() returns {}
            # rather than substituting Parler's own fallback language, so
            # get_display_value() yields None, never the German text.
            self.assertIsNone(u_de.display_intro)


class ComparisonSnapshotLanguageIsolationTests(TestCase):
    """Comparison has no display_intro/get_display_value - tested directly
    against get_live_value(), the shared mixin method compare/views.py's
    ComparisonDetailView actually calls (see module docstring)."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="comparison-snapshot-author", password="pass")

    def test_empty_field_in_requested_language_snapshot_stays_empty(self):
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        c.create_translation("en", title="T", intro="", body="b", slug="cmp-leak-en")
        c.create_translation("de", title="T", intro="Deutsche Einleitung", body="b", slug="cmp-leak-de")
        c.publish(by=self.author)
        c.save()
        c = Comparison.objects.get(pk=c.pk)

        self.assertEqual(c.get_live_value("intro", "en"), "")

    def test_requested_language_missing_from_nonempty_snapshot_stays_empty_no_draft_leak(self):
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        c.create_translation("de", title="T", intro="Deutscher Text", body="b", slug="cmp-de-only")
        c.publish(by=self.author)
        c.save()
        c = Comparison.objects.get(pk=c.pk)
        self.assertNotIn("en", c.live_i18n)

        c.create_translation("en", title="Draft EN", intro="Draft EN intro", body="b", slug="cmp-de-only-en-draft")
        c.save()
        c = Comparison.objects.get(pk=c.pk)

        self.assertEqual(c.get_live_value("intro", "en"), "")

    def test_legacy_missing_snapshot_returns_none_signaling_no_authoritative_value(self):
        # Comparison does not implement a same-language draft fallback for
        # get_live_value() itself (that is left to callers, e.g.
        # compare/views.py's own `or safe_translation_getter(...)`), so the
        # real contract here is simply: no snapshot at all -> None.
        c = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        c.create_translation("en", title="T", intro="Draft intro", body="b", slug="cmp-legacy-en")
        self.assertEqual(c.live_i18n, {})

        self.assertIsNone(c.get_live_value("intro", "en"))
