"""
Beta 8.8: documents Tool's current, deliberate cross-language fallback
behavior. Tool.slug is a SHARED (non-translated) model field - unlike
Prompt/Guide/Comparison, a Tool has exactly one URL regardless of language,
so "wrong-language detail page under the wrong prefix" is not a coherent
bug category here. Combined with PARLER_LANGUAGES' project-wide
hide_untranslated=False, an EN-only tool is expected to keep showing
(via fallback content) on the German catalog rather than disappearing.
This is intentionally NOT changed in Beta 8.8 - see the slice report.
"""
from django.test import TestCase
from django.utils import timezone

from catalog.models import Tool


class ToolLanguageFallbackIsIntentionalTests(TestCase):
    def test_slug_is_a_shared_field_not_a_translated_one(self):
        translated_field_names = {f.name for f in Tool._parler_meta.root_model._meta.get_fields()}
        self.assertNotIn("slug", translated_field_names)

    def test_en_only_tool_still_appears_on_the_german_catalog_list(self):
        from catalog.views import ToolListView
        from django.test import RequestFactory
        from django.utils import translation

        t = Tool.objects.create(slug="en-only-fallback-tool", published_at=timezone.now())
        t.create_translation("en", name="EN Only Tool")

        with translation.override("de"):
            request = RequestFactory().get("/catalog/")
            view = ToolListView()
            view.request = request
            self.assertTrue(view.get_queryset().filter(pk=t.pk).exists())

    def test_en_only_tool_detail_page_still_renders_under_german_prefix(self):
        t = Tool.objects.create(slug="en-only-fallback-detail", published_at=timezone.now())
        t.create_translation("en", name="EN Only Detail Tool")

        resp = self.client.get("/de/catalog/en-only-fallback-detail/")
        self.assertEqual(resp.status_code, 200)
