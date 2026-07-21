from django.conf import settings
from django.test import SimpleTestCase
from django.urls import resolve, reverse
from django.utils import translation

from search.views import SearchResultsView


class SearchUrlTests(SimpleTestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def test_reverse_produces_the_language_prefixed_path(self):
        for language_code, expected in (("en", "/en/search/"), ("de", "/de/search/")):
            with self.subTest(language=language_code):
                with translation.override(language_code):
                    self.assertEqual(reverse("search:results"), expected)

    def test_paths_resolve_to_the_search_view(self):
        # resolve() builds the i18n_patterns prefix from the *active*
        # language, so each path has to be resolved under its own.
        for language_code, path in (("en", "/en/search/"), ("de", "/de/search/")):
            with self.subTest(path=path):
                with translation.override(language_code):
                    match = resolve(path)
                self.assertEqual(match.view_name, "search:results")
                self.assertIs(match.func.view_class, SearchResultsView)

    def test_namespace_and_url_name(self):
        with translation.override("en"):
            match = resolve("/en/search/")
        self.assertEqual(match.namespace, "search")
        self.assertEqual(match.url_name, "results")

    def test_there_is_exactly_one_search_endpoint(self):
        from search import urls

        self.assertEqual(len(urls.urlpatterns), 1)
        self.assertEqual(urls.app_name, "search")
