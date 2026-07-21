from django.test import SimpleTestCase

from search.registry import SEARCH_ADAPTERS
from search.result_types import SearchResultKind


class RegistryTests(SimpleTestCase):
    def test_is_a_tuple(self):
        self.assertIsInstance(SEARCH_ADAPTERS, tuple)

    def test_covers_every_searchable_kind_exactly_once(self):
        kinds = [adapter.kind for adapter in SEARCH_ADAPTERS]
        self.assertEqual(set(kinds), set(SearchResultKind))
        self.assertEqual(len(kinds), len(set(kinds)))

    def test_holds_exactly_five_adapters(self):
        self.assertEqual(len(SEARCH_ADAPTERS), 5)

    def test_contains_no_glossary_adapter(self):
        names = {type(adapter).__name__ for adapter in SEARCH_ADAPTERS}
        self.assertFalse({name for name in names if "Glossary" in name})

    def test_adapters_are_stateless(self):
        for adapter in SEARCH_ADAPTERS:
            with self.subTest(adapter=type(adapter).__name__):
                self.assertEqual(vars(adapter), {})

    def test_every_adapter_exposes_a_search_callable(self):
        for adapter in SEARCH_ADAPTERS:
            with self.subTest(adapter=type(adapter).__name__):
                self.assertTrue(callable(adapter.search))
