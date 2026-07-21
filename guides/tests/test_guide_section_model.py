"""
Coverage-Schritt 3/3a: GuideSection (guides/models.py) - its own
_current_values_for/get_live_value/get_display_value/display_title/
display_body methods had no dedicated test coverage before Coverage-Schritt 3
(test_signals.py creates GuideSection rows only to test the auto-move-to-
review signal, not this display/snapshot logic).

Coverage-Schritt 3a: GuideSection.get_live_value() independently duplicated
the same cross-language snapshot leak as EditorialWorkflowMixin.get_live_value()
(core/models/editorial.py) - fixed to delegate to the same shared, pure
core.models.editorial.get_snapshot_field() helper. Its own _current_values_for()
legacy-fallback path is now also guarded with has_translation(lang) so it
never silently substitutes another language's draft text via Parler's own
PARLER_LANGUAGES fallback.
"""
from django.test import TestCase
from django.utils import translation

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide, GuideSection


def make_section(**translations):
    guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
    section = GuideSection.objects.create(guide=guide, order=1)
    for lang, (title, body) in translations.items():
        section.create_translation(lang, title=title, body=body)
    return section


class GuideSectionDisplayValueTests(TestCase):
    def test_display_title_and_body_use_current_translation_without_a_snapshot(self):
        section = make_section(en=("Draft Title", "Draft Body"))
        with translation.override("en"):
            self.assertEqual(section.display_title, "Draft Title")
            self.assertEqual(section.display_body, "Draft Body")

    def test_live_snapshot_value_wins_over_current_draft(self):
        section = make_section(en=("Draft Title", "Draft Body"))
        section.live_i18n = {"en": {"title": "Live Title", "body": "Live Body"}}
        section.save(update_fields=["live_i18n"])
        with translation.override("en"):
            self.assertEqual(section.display_title, "Live Title")
            self.assertEqual(section.display_body, "Live Body")

    def test_no_translation_and_no_snapshot_returns_none(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        section = GuideSection.objects.create(guide=guide, order=1)
        with translation.override("en"):
            self.assertIsNone(section.get_live_value("title"))

    def test_get_live_value_returns_none_when_live_i18n_is_empty_dict(self):
        section = make_section(en=("Draft Title", "Draft Body"))
        self.assertEqual(section.live_i18n, {})
        with translation.override("en"):
            self.assertIsNone(section.get_live_value("title"))

    def test_str_includes_guide_id_order_and_title(self):
        section = make_section(en=("My Section", "Body"))
        with translation.override("en"):
            text = str(section)
        self.assertIn(str(section.guide_id), text)
        self.assertIn("1", text)
        self.assertIn("My Section", text)


class GuideSectionSnapshotLanguageIsolationTests(TestCase):
    """The Coverage-Schritt-3a snapshot contract for GuideSection, exercised
    through the public display_title/display_body properties."""

    def test_empty_en_field_with_de_set_stays_empty(self):
        section = make_section(en=("Title", "Draft Body"))
        section.live_i18n = {"en": {"body": ""}, "de": {"body": "Deutscher Text"}}
        section.save(update_fields=["live_i18n"])
        with translation.override("en"):
            self.assertEqual(section.display_body, "")

    def test_missing_en_key_with_de_value_present_stays_empty(self):
        section = make_section(en=("Title", "Draft Body"))
        section.live_i18n = {"en": {"title": "Live Title"}, "de": {"body": "Deutscher Text"}}
        section.save(update_fields=["live_i18n"])
        with translation.override("en"):
            self.assertEqual(section.display_body, "")

    def test_en_missing_from_nonempty_snapshot_stays_empty(self):
        section = make_section(en=("Draft Title", "Draft Body"))
        section.live_i18n = {"de": {"title": "Deutscher Titel", "body": "Deutscher Text"}}
        section.save(update_fields=["live_i18n"])
        with translation.override("en"):
            self.assertEqual(section.display_title, "")
            self.assertEqual(section.display_body, "")

    def test_empty_snapshot_uses_current_en_section_values(self):
        section = make_section(en=("Draft Title", "Draft Body"))
        self.assertEqual(section.live_i18n, {})
        with translation.override("en"):
            self.assertEqual(section.display_title, "Draft Title")
            self.assertEqual(section.display_body, "Draft Body")

    def test_empty_snapshot_with_only_de_translation_gives_empty_en(self):
        section = make_section(de=("Deutscher Titel", "Deutscher Text"))
        self.assertEqual(section.live_i18n, {})
        with translation.override("en"):
            # No "en" translation exists - _current_values_for() returns {}
            # rather than substituting Parler's own fallback language, so
            # get_display_value() yields None (its established "nothing
            # available" contract), never the German text.
            self.assertIsNone(section.display_title)
            self.assertIsNone(section.display_body)
