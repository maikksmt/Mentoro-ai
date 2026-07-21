"""
Beta 10.6: the strictly language-bound public tool projection.

The catalogue deliberately falls back across languages; this projection does
not. These tests pin that difference, because the global search depends on it.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone, translation

from catalog.models import Tool, ToolTranslation
from catalog.projections import (
    PublicToolProjection,
    project_public_tool,
    public_tool_url,
)


def make_tool(slug, *, translations, vendor="", published_at=None):
    tool = Tool.objects.create(
        slug=slug,
        vendor=vendor,
        published_at=published_at or timezone.now() - timedelta(days=1),
    )
    for language_code, values in translations.items():
        tool.create_translation(
            language_code,
            name=values.get("name", "Tool"),
            short_description=values.get("short_description", ""),
            long_description=values.get("long_description", ""),
        )
    return tool


def translation_row(tool, language_code):
    return ToolTranslation.objects.select_related("master").get(
        master=tool, language_code=language_code
    )


class ProjectPublicToolTests(TestCase):
    def test_projects_the_requested_language(self):
        tool = make_tool(
            "projection-bilingual",
            vendor="OpenAI",
            translations={
                "en": {
                    "name": "English Name",
                    "short_description": "English summary",
                    "long_description": "English body",
                },
                "de": {
                    "name": "Deutscher Name",
                    "short_description": "Deutsche Zusammenfassung",
                    "long_description": "Deutscher Text",
                },
            },
        )
        projection = project_public_tool(
            translation_row(tool, "de"), language_code="de"
        )
        self.assertIsInstance(projection, PublicToolProjection)
        self.assertEqual(projection.title, "Deutscher Name")
        self.assertEqual(projection.summary, "Deutsche Zusammenfassung")
        self.assertEqual(projection.body, "Deutscher Text")
        self.assertEqual(projection.language_code, "de")
        self.assertEqual(projection.object_id, tool.pk)
        self.assertEqual(projection.vendor, "OpenAI")
        self.assertEqual(projection.url, "/de/catalog/projection-bilingual/")

    def test_ignores_the_ambient_language(self):
        tool = make_tool(
            "projection-ambient",
            translations={
                "en": {"name": "English Name"},
                "de": {"name": "Deutscher Name"},
            },
        )
        with translation.override("en"):
            projection = project_public_tool(
                translation_row(tool, "de"), language_code="de"
            )
        self.assertEqual(projection.title, "Deutscher Name")
        self.assertEqual(projection.url, "/de/catalog/projection-ambient/")

    def test_rejects_a_translation_from_another_language(self):
        tool = make_tool(
            "projection-mismatch", translations={"en": {"name": "English Name"}}
        )
        with self.assertRaisesMessage(ValueError, "expected 'de'"):
            project_public_tool(translation_row(tool, "en"), language_code="de")

    def test_language_is_required(self):
        tool = make_tool(
            "projection-required", translations={"en": {"name": "English Name"}}
        )
        with self.assertRaisesMessage(ValueError, "language_code is required"):
            project_public_tool(translation_row(tool, "en"), language_code="")

    def test_carries_publication_dates(self):
        tool = make_tool(
            "projection-dates", translations={"en": {"name": "Dated Tool"}}
        )
        projection = project_public_tool(
            translation_row(tool, "en"), language_code="en"
        )
        self.assertEqual(projection.published_at, tool.published_at)
        self.assertEqual(projection.updated_at, tool.updated_at)

    def test_is_immutable(self):
        from dataclasses import FrozenInstanceError

        tool = make_tool(
            "projection-immutable", translations={"en": {"name": "Immutable"}}
        )
        projection = project_public_tool(
            translation_row(tool, "en"), language_code="en"
        )
        with self.assertRaises(FrozenInstanceError):
            projection.title = "changed"

    def test_empty_texts_become_empty_strings(self):
        tool = make_tool("projection-empty", translations={"en": {"name": "Only Name"}})
        projection = project_public_tool(
            translation_row(tool, "en"), language_code="en"
        )
        self.assertEqual(projection.summary, "")
        self.assertEqual(projection.body, "")
        self.assertEqual(projection.vendor, "")


class PublicToolUrlTests(TestCase):
    def test_uses_the_shared_slug_with_the_requested_prefix(self):
        tool = make_tool(
            "url-shared-slug",
            translations={"en": {"name": "EN"}, "de": {"name": "DE"}},
        )
        self.assertEqual(
            public_tool_url(tool, language_code="en"), "/en/catalog/url-shared-slug/"
        )
        self.assertEqual(
            public_tool_url(tool, language_code="de"), "/de/catalog/url-shared-slug/"
        )

    def test_prefix_follows_the_requested_language_not_the_ambient_one(self):
        tool = make_tool("url-ambient", translations={"en": {"name": "EN"}})
        with translation.override("de"):
            self.assertEqual(
                public_tool_url(tool, language_code="en"), "/en/catalog/url-ambient/"
            )

    def test_never_returns_the_external_website(self):
        tool = Tool.objects.create(
            slug="url-external",
            website="https://example.test/product",
            published_at=timezone.now() - timedelta(days=1),
        )
        tool.create_translation(
            "en", name="External", short_description="", long_description=""
        )
        url = public_tool_url(tool, language_code="en")
        self.assertNotIn("example.test", url)
        self.assertTrue(url.startswith("/en/catalog/"))

    def test_language_is_required(self):
        tool = make_tool("url-required", translations={"en": {"name": "EN"}})
        with self.assertRaisesMessage(ValueError, "language_code is required"):
            public_tool_url(tool, language_code="")
