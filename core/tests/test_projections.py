"""
Beta 10.4: the public content projection API extracted from the teaser
helpers, and the equivalence of its Python and database expressions.

The existing teaser contract itself is covered by
core/tests/test_public_teaser.py; this module only pins the boundaries the
extraction created - the public API's explicit-language requirement, its
fail-closed behaviour, and the guarantee that the SQL twin used by search
resolves to exactly the same value as the Python function used by teasers.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import translation
from parler.utils.context import switch_language

from core.models.editorial import EditorialWorkflowMixin
from core.projections import (
    PublicContentProjection,
    project_public_content,
    public_content_url,
    public_content_value,
    public_content_value_expression,
)
from core.services import to_teaser_item
from guides.models import Guide, GuideTranslation

User = get_user_model()


def make_published_guide(*, author, translations):
    """Publishes a guide through the real FSM, so live_i18n is written the
    same way production writes it."""
    guide = Guide.objects.create(
        status=EditorialWorkflowMixin.STATUS_APPROVED, author=author
    )
    for language_code, values in translations.items():
        guide.create_translation(language_code, **values)
    guide.publish(by=author)
    guide.save()
    return guide


def begin_unpublished_revision(guide, *, author, language_code, **fields):
    """Edits the current translation without republishing, so the draft and
    the published snapshot diverge."""
    with switch_language(guide, language_code):
        for name, value in fields.items():
            setattr(guide, name, value)
        guide.save()
    guide.move_to_review(by=author)
    guide.last_published_revision_id = 1
    guide.save()


class ProjectionBaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="projection-editor",
            email="projection@example.com",
            password="testpass123",
        )


class PublicContentValueTests(ProjectionBaseTests):
    def test_returns_published_value(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Public Title",
                    "intro": "Public intro",
                    "body": "Public body",
                    "slug": "projection-public-en",
                }
            },
        )
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "Public Title"
        )

    def test_ignores_unpublished_draft_value(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Public Title",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-draft-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", title="Draft Title"
        )
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "Public Title"
        )

    def test_falls_back_to_translation_without_snapshot(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en", title="No Snapshot", intro="i", body="b", slug="projection-nosnap-en"
        )
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "No Snapshot"
        )

    def test_missing_translation_yields_empty_string(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Only English",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-enonly-en",
                }
            },
        )
        self.assertEqual(public_content_value(guide, "title", language_code="de"), "")

    def test_never_substitutes_another_language(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English Title",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-lang-en",
                },
                "de": {
                    "title": "Deutscher Titel",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-lang-de",
                },
            },
        )
        self.assertEqual(
            public_content_value(guide, "title", language_code="de"), "Deutscher Titel"
        )
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "English Title"
        )

    def test_language_is_required(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {"title": "T", "intro": "i", "body": "b", "slug": "projection-req-en"}
            },
        )
        for language_code in ("", None):
            with self.subTest(language_code=language_code), self.assertRaisesMessage(ValueError, "language_code is required"):
                public_content_value(guide, "title", language_code=language_code)

    def test_ignores_ambient_language(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English Title",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-ambient-en",
                },
                "de": {
                    "title": "Deutscher Titel",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-ambient-de",
                },
            },
        )
        with translation.override("en"):
            self.assertEqual(
                public_content_value(guide, "title", language_code="de"),
                "Deutscher Titel",
            )


class PublicContentUrlTests(ProjectionBaseTests):
    def test_uses_published_slug_not_draft_slug(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "T",
                    "intro": "i",
                    "body": "b",
                    "slug": "projection-url-public-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", slug="projection-url-draft-en"
        )
        url = public_content_url(guide, language_code="en")
        self.assertIn("projection-url-public-en", url)
        self.assertNotIn("draft", url)

    def test_language_is_required(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {"title": "T", "intro": "i", "body": "b", "slug": "projection-urlreq-en"}
            },
        )
        with self.assertRaisesMessage(ValueError, "language_code is required"):
            public_content_url(guide, language_code="")


class ProjectPublicContentTests(ProjectionBaseTests):
    def test_projects_every_public_value_from_one_language(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "de": {
                    "title": "Deutscher Titel",
                    "intro": "Deutsches Intro",
                    "body": "Deutscher Body",
                    "slug": "projection-full-de",
                },
                "en": {
                    "title": "English Title",
                    "intro": "English intro",
                    "body": "English body",
                    "slug": "projection-full-en",
                },
            },
        )
        projection = project_public_content(guide, "guide", language_code="de")
        self.assertIsInstance(projection, PublicContentProjection)
        self.assertEqual(projection.title, "Deutscher Titel")
        self.assertEqual(projection.summary, "Deutsches Intro")
        self.assertEqual(projection.body, "Deutscher Body")
        self.assertIn("projection-full-de", projection.url)
        self.assertEqual(projection.language_code, "de")
        self.assertEqual(projection.object_id, guide.pk)
        self.assertEqual(projection.kind, "guide")

    def test_projection_is_immutable(self):
        from dataclasses import FrozenInstanceError

        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {"title": "T", "intro": "i", "body": "b", "slug": "projection-imm-en"}
            },
        )
        projection = project_public_content(guide, "guide", language_code="en")
        with self.assertRaises(FrozenInstanceError):
            projection.title = "changed"


class PythonAndDatabaseExpressionAgreeTests(ProjectionBaseTests):
    """
    The search adapter matches against the database expression while results
    are rendered from the Python function. If they ever disagreed, search
    could match text that is not the text shown - so this asserts they agree
    across exactly the states that make them differ.
    """

    def _assert_agrees(self, guide, language_code):
        annotated = (
            Guide.objects.filter(pk=guide.pk)
            .annotate(
                **{
                    f"public_{field}": public_content_value_expression(
                        GuideTranslation, field, language_code=language_code
                    )
                    for field in ("title", "intro", "body")
                }
            )
            .values("public_title", "public_intro", "public_body")
            .get()
        )
        for field in ("title", "intro", "body"):
            with self.subTest(field=field, language=language_code):
                self.assertEqual(
                    annotated[f"public_{field}"],
                    public_content_value(guide, field, language_code=language_code),
                )

    def test_agree_for_published_guide(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Agree Title",
                    "intro": "Agree intro",
                    "body": "Agree body",
                    "slug": "agree-published-en",
                }
            },
        )
        self._assert_agrees(guide, "en")

    def test_agree_when_draft_diverges_from_snapshot(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Agree Public",
                    "intro": "Public intro",
                    "body": "Public body",
                    "slug": "agree-diverge-en",
                }
            },
        )
        begin_unpublished_revision(
            guide,
            author=self.author,
            language_code="en",
            title="Agree Draft",
            intro="Draft intro",
            body="Draft body",
        )
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment.
        self._assert_agrees(Guide.objects.get(pk=guide.pk), "en")

    def test_agree_without_snapshot(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en", title="No Snap", intro="i", body="b", slug="agree-nosnap-en"
        )
        self._assert_agrees(guide, "en")

    def test_agree_for_both_languages(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English",
                    "intro": "English intro",
                    "body": "English body",
                    "slug": "agree-both-en",
                },
                "de": {
                    "title": "Deutsch",
                    "intro": "Deutsches Intro",
                    "body": "Deutscher Body",
                    "slug": "agree-both-de",
                },
            },
        )
        self._assert_agrees(guide, "en")
        self._assert_agrees(guide, "de")

    def test_agree_when_a_published_field_is_empty(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Agree empty",
                    "intro": "",
                    "body": "",
                    "slug": "agree-empty-en",
                }
            },
        )
        begin_unpublished_revision(
            guide,
            author=self.author,
            language_code="en",
            intro="Draft intro",
            body="Draft body",
        )
        self._assert_agrees(Guide.objects.get(pk=guide.pk), "en")

    def test_agree_when_a_snapshot_field_is_absent(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Agree absent",
                    "intro": "Published intro",
                    "body": "b",
                    "slug": "agree-absent-en",
                }
            },
        )
        snapshot = dict(guide.live_i18n)
        snapshot["en"] = {k: v for k, v in snapshot["en"].items() if k != "intro"}
        Guide.objects.filter(pk=guide.pk).update(live_i18n=snapshot)
        self._assert_agrees(Guide.objects.get(pk=guide.pk), "en")

    def test_agree_when_the_requested_language_has_no_snapshot(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Agree missing lang",
                    "intro": "i",
                    "body": "b",
                    "slug": "agree-missing-en",
                }
            },
        )
        guide.create_translation(
            "de", title="Draft Titel", intro="Draft Intro", body="Draft Body",
            slug="agree-missing-de",
        )
        self._assert_agrees(Guide.objects.get(pk=guide.pk), "de")

    def test_agree_for_a_completely_empty_legacy_snapshot(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en", title="Agree legacy", intro="i", body="b", slug="agree-legacy-en"
        )
        self.assertEqual(guide.live_i18n, {})
        self._assert_agrees(guide, "en")

    def test_expression_requires_a_language(self):
        with self.assertRaisesMessage(ValueError, "language_code is required"):
            public_content_value_expression(GuideTranslation, "title", language_code="")


class SnapshotAuthorityTests(ProjectionBaseTests):
    """
    A published snapshot is the sole authority for its language. An empty or
    absent field in it is a published value, not a missing one, and must never
    fall through to the draft translation.
    """

    def test_empty_published_intro_stays_empty_when_draft_fills_it(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Empty intro case",
                    "intro": "",
                    "body": "b",
                    "slug": "empty-intro-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", intro="Draftneedle intro"
        )
        self.assertEqual(public_content_value(guide, "intro", language_code="en"), "")

    def test_empty_published_body_stays_empty_when_draft_fills_it(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Empty body case",
                    "intro": "i",
                    "body": "",
                    "slug": "empty-body-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", body="Draftneedle body"
        )
        self.assertEqual(public_content_value(guide, "body", language_code="en"), "")

    def test_field_absent_from_the_snapshot_mapping_is_treated_as_empty(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Absent field case",
                    "intro": "Published intro",
                    "body": "b",
                    "slug": "absent-field-en",
                }
            },
        )
        # Simulate a snapshot written before `intro` was part of
        # LIVE_SNAPSHOT_FIELDS: the key is missing entirely.
        snapshot = dict(guide.live_i18n)
        snapshot["en"] = {k: v for k, v in snapshot["en"].items() if k != "intro"}
        Guide.objects.filter(pk=guide.pk).update(live_i18n=snapshot)
        guide = Guide.objects.get(pk=guide.pk)

        self.assertEqual(public_content_value(guide, "intro", language_code="en"), "")
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "Absent field case"
        )

    def test_language_added_after_publish_has_no_public_revision(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English published",
                    "intro": "English intro",
                    "body": "English body",
                    "slug": "late-de-en",
                }
            },
        )
        guide.create_translation(
            "de",
            title="Draftneedle Titel",
            intro="Draftneedle Intro",
            body="Draftneedle Body",
            slug="draftneedle-de",
        )
        guide = Guide.objects.get(pk=guide.pk)

        for field in ("title", "intro", "body", "slug", "public_slug"):
            with self.subTest(field=field):
                self.assertEqual(
                    public_content_value(guide, field, language_code="de"), ""
                )
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"),
            "English published",
        )

    def test_language_added_after_publish_yields_no_public_url(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English published",
                    "intro": "i",
                    "body": "b",
                    "slug": "late-url-en",
                }
            },
        )
        guide.create_translation(
            "de", title="Titel", intro="i", body="b", slug="draftneedle-url-de"
        )
        guide = Guide.objects.get(pk=guide.pk)

        url = public_content_url(guide, language_code="de")
        self.assertEqual(url, "#")
        self.assertNotIn("draftneedle-url-de", url)
        self.assertNotIn("late-url-en", url)

    def test_mirrored_case_english_added_after_german_publish(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "de": {
                    "title": "Deutsch veröffentlicht",
                    "intro": "Deutsches Intro",
                    "body": "Deutscher Body",
                    "slug": "late-en-de",
                }
            },
        )
        guide.create_translation(
            "en",
            title="Draftneedle title",
            intro="Draftneedle intro",
            body="Draftneedle body",
            slug="draftneedle-late-en",
        )
        guide = Guide.objects.get(pk=guide.pk)

        for field in ("title", "intro", "body", "slug"):
            with self.subTest(field=field):
                self.assertEqual(
                    public_content_value(guide, field, language_code="en"), ""
                )
        self.assertEqual(public_content_url(guide, language_code="en"), "#")
        self.assertEqual(
            public_content_value(guide, "title", language_code="de"),
            "Deutsch veröffentlicht",
        )

    def test_ambient_language_does_not_change_the_verdict(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English published",
                    "intro": "i",
                    "body": "b",
                    "slug": "ambient-late-en",
                }
            },
        )
        guide.create_translation(
            "de", title="Draftneedle", intro="i", body="b", slug="ambient-late-de"
        )
        guide = Guide.objects.get(pk=guide.pk)

        for ambient in ("en", "de"):
            with self.subTest(ambient=ambient), translation.override(ambient):
                self.assertEqual(
                    public_content_value(guide, "title", language_code="de"), ""
                )
                self.assertEqual(public_content_url(guide, language_code="de"), "#")

    def test_legacy_record_without_any_snapshot_still_resolves(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en", title="Legacy title", intro="Legacy intro", body="b", slug="legacy-en"
        )
        self.assertEqual(guide.live_i18n, {})
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "Legacy title"
        )
        self.assertIn("legacy-en", public_content_url(guide, language_code="en"))

    def test_legacy_record_still_has_no_cross_language_fallback(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en", title="Legacy only", intro="i", body="b", slug="legacy-cross-en"
        )
        self.assertEqual(public_content_value(guide, "title", language_code="de"), "")
        self.assertEqual(public_content_url(guide, language_code="de"), "#")

    def test_regular_published_guide_is_unaffected(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Regular title",
                    "intro": "Regular intro",
                    "body": "Regular body",
                    "slug": "regular-en",
                }
            },
        )
        self.assertEqual(
            public_content_value(guide, "title", language_code="en"), "Regular title"
        )
        self.assertEqual(
            public_content_url(guide, language_code="en"), "/en/guides/regular-en/"
        )


class PublicUrlLanguagePrefixTests(ProjectionBaseTests):
    def test_url_prefix_follows_the_requested_language_not_the_ambient_one(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "T",
                    "intro": "i",
                    "body": "b",
                    "slug": "prefix-en",
                },
                "de": {
                    "title": "T",
                    "intro": "i",
                    "body": "b",
                    "slug": "prefix-de",
                },
            },
        )
        with translation.override("en"):
            self.assertEqual(
                public_content_url(guide, language_code="de"), "/de/guides/prefix-de/"
            )
        with translation.override("de"):
            self.assertEqual(
                public_content_url(guide, language_code="en"), "/en/guides/prefix-en/"
            )


class TeaserCompatibilityTests(ProjectionBaseTests):
    """The extraction must not have changed to_teaser_item()'s output."""

    def test_teaser_keys_are_unchanged(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Teaser Title",
                    "intro": "Teaser intro",
                    "body": "b",
                    "slug": "teaser-keys-en",
                }
            },
        )
        with translation.override("en"):
            item = to_teaser_item(guide, "guide")
        self.assertEqual(set(item), {"title", "teaser", "url", "date", "badge"})

    def test_teaser_values_come_from_the_published_revision(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Teaser Public",
                    "intro": "Public teaser intro",
                    "body": "b",
                    "slug": "teaser-public-en",
                }
            },
        )
        begin_unpublished_revision(
            guide,
            author=self.author,
            language_code="en",
            title="Teaser Draft",
            intro="Draft teaser intro",
            slug="teaser-draft-en",
        )
        with translation.override("en"):
            item = to_teaser_item(guide, "guide")
        self.assertEqual(item["title"], "Teaser Public")
        self.assertEqual(item["teaser"], "Public teaser intro")
        self.assertIn("teaser-public-en", item["url"])
        self.assertEqual(item["badge"], "Guide")

    def test_teaser_url_is_not_a_placeholder_for_a_public_guide(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "T",
                    "intro": "i",
                    "body": "b",
                    "slug": "teaser-url-en",
                }
            },
        )
        with translation.override("en"):
            item = to_teaser_item(guide, "guide")
        self.assertNotEqual(item["url"], "#")
        self.assertTrue(item["url"].startswith("/en/guides/"))

    def test_teaser_respects_an_explicit_language(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "English Teaser",
                    "intro": "i",
                    "body": "b",
                    "slug": "teaser-lang-en",
                },
                "de": {
                    "title": "Deutscher Teaser",
                    "intro": "i",
                    "body": "b",
                    "slug": "teaser-lang-de",
                },
            },
        )
        with translation.override("en"):
            item = to_teaser_item(guide, "guide", language_code="de")
        self.assertEqual(item["title"], "Deutscher Teaser")
        self.assertIn("teaser-lang-de", item["url"])
