"""
Beta 10.6: the tool search adapter.

Two guarantees carry this slice. Visibility is exactly ``Tool.objects.public()``
- a pure ``published_at <= now`` gate, so a scheduled tool is invisible through
every field including its vendor and categories. And the search is strictly
language-bound: unlike the catalogue, which deliberately shows an English-only
tool on German pages via parler's fallback, the global search does not find it
there at all.

Requires PostgreSQL.
"""
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.conf import settings
from django.test import TestCase
from django.utils import timezone, translation

from catalog.models import Category, Tool
from search.adapters.tools import TOOL_SEARCH_FIELDS, ToolSearchAdapter
from search.query import NormalizedSearchQuery, SearchQueryIssue, normalize_search_query
from search.result_types import SearchMatchedField, SearchResult, SearchResultKind

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)

PAST = timedelta(days=1)
FUTURE = timedelta(days=30)


def make_tool(slug, *, translations, vendor="", published_at=None, categories=()):
    tool = Tool.objects.create(
        slug=slug,
        vendor=vendor,
        published_at=published_at or timezone.now() - PAST,
    )
    for language_code, values in translations.items():
        tool.create_translation(
            language_code,
            name=values.get("name", "Neutral tool name"),
            short_description=values.get("short_description", ""),
            long_description=values.get("long_description", ""),
        )
    for category in categories:
        tool.categories.add(category)
    return tool


def make_category(*, translations):
    category = Category.objects.create()
    for language_code, values in translations.items():
        category.create_translation(
            language_code, name=values["name"], slug=values["slug"], description=""
        )
    return category


class ToolAdapterTestCase(TestCase):
    def setUp(self):
        self.adapter = ToolSearchAdapter()

    def search(self, term, language_code="en"):
        return self.adapter.search(
            query=normalize_search_query(term), language_code=language_code
        )

    def ids(self, term, language_code="en"):
        return [result.object_id for result in self.search(term, language_code)]


class ToolFieldConfigurationTests(TestCase):
    def test_searchable_fields_and_weights(self):
        self.assertEqual(
            [(f.key, f.weight, f.matched_field) for f in TOOL_SEARCH_FIELDS],
            [
                ("name", "A", SearchMatchedField.TITLE),
                ("short_description", "B", SearchMatchedField.SUMMARY),
                ("long_description", "C", SearchMatchedField.BODY),
                ("vendor", "B", SearchMatchedField.METADATA),
                ("category_names", "D", SearchMatchedField.METADATA),
            ],
        )

    def test_filter_and_display_fields_are_not_searchable(self):
        searchable = {f.sql_source for f in TOOL_SEARCH_FIELDS}
        for excluded in (
            "master__website",
            "master__pricing_model",
            "master__free_tier",
            "master__rating",
            "master__is_featured",
            "master__tags",
        ):
            with self.subTest(field=excluded):
                self.assertNotIn(excluded, searchable)

    def test_adapter_kind(self):
        self.assertIs(ToolSearchAdapter.kind, SearchResultKind.TOOL)


@postgresql_only
class VisibilityTests(ToolAdapterTestCase):
    def test_public_tool_is_found(self):
        tool = make_tool(
            "vis-public",
            translations={
                "en": {
                    "name": "Publictoken Studio",
                    "short_description": "A summary",
                    "long_description": "A body",
                }
            },
        )
        results = self.search("Publictoken")
        self.assertEqual([r.object_id for r in results], [tool.pk])
        result = results[0]
        self.assertIs(result.kind, SearchResultKind.TOOL)
        self.assertEqual(result.title, "Publictoken Studio")
        self.assertEqual(result.language_code, "en")
        self.assertEqual(result.url, "/en/catalog/vis-public/")

    def test_future_tool_is_never_found_through_any_field(self):
        category = make_category(
            translations={
                "en": {"name": "Futuretoken Category", "slug": "future-cat-en"},
                "de": {"name": "Futuretoken Kategorie", "slug": "future-cat-de"},
            }
        )
        tool = make_tool(
            "vis-future",
            published_at=timezone.now() + FUTURE,
            vendor="Futuretoken Vendor",
            categories=[category],
            translations={
                "en": {
                    "name": "Futuretoken name",
                    "short_description": "Futuretoken summary",
                    "long_description": "Futuretoken body",
                }
            },
        )
        self.assertNotIn(tool.pk, self.ids("Futuretoken"))
        self.assertEqual(self.ids("Futuretoken"), [])

    def test_publication_boundary_matches_the_public_queryset(self):
        past = make_tool(
            "vis-past",
            published_at=timezone.now() - PAST,
            translations={"en": {"name": "Boundarytoken past"}},
        )
        future = make_tool(
            "vis-scheduled",
            published_at=timezone.now() + FUTURE,
            translations={"en": {"name": "Boundarytoken scheduled"}},
        )
        public_ids = set(Tool.objects.public().values_list("pk", flat=True))
        self.assertIn(past.pk, public_ids)
        self.assertNotIn(future.pk, public_ids)
        self.assertEqual(self.ids("Boundarytoken"), [past.pk])

    def test_results_are_a_subset_of_the_public_queryset(self):
        make_tool("vis-sub-a", translations={"en": {"name": "Subsettoken one"}})
        make_tool("vis-sub-b", translations={"en": {"name": "Subsettoken two"}})
        make_tool(
            "vis-sub-c",
            published_at=timezone.now() + FUTURE,
            translations={"en": {"name": "Subsettoken three"}},
        )
        public_ids = set(Tool.objects.public().values_list("pk", flat=True))
        self.assertTrue(set(self.ids("Subsettoken")).issubset(public_ids))
        self.assertEqual(len(self.ids("Subsettoken")), 2)


@postgresql_only
class LanguageIsolationTests(ToolAdapterTestCase):
    def _bilingual(self):
        return make_tool(
            "lang-bilingual",
            translations={
                "en": {
                    "name": "Englishonly Studio",
                    "short_description": "English summary",
                    "long_description": "English body",
                },
                "de": {
                    "name": "Deutschonly Studio",
                    "short_description": "Deutsche Zusammenfassung",
                    "long_description": "Deutscher Text",
                },
            },
        )

    def test_english_token_does_not_match_german_search(self):
        tool = self._bilingual()
        self.assertIn(tool.pk, self.ids("Englishonly", "en"))
        self.assertNotIn(tool.pk, self.ids("Englishonly", "de"))

    def test_german_token_does_not_match_english_search(self):
        tool = self._bilingual()
        self.assertIn(tool.pk, self.ids("Deutschonly", "de"))
        self.assertNotIn(tool.pk, self.ids("Deutschonly", "en"))

    def test_tool_without_a_german_translation_is_not_found_in_german(self):
        # The catalogue shows this tool on German pages with its English text;
        # the global search deliberately does not find it there.
        tool = make_tool(
            "lang-en-only",
            translations={
                "en": {
                    "name": "Fallbacktoken Studio",
                    "short_description": "English summary",
                    "long_description": "English body",
                }
            },
        )
        self.assertIn(tool.pk, self.ids("Fallbacktoken", "en"))
        self.assertEqual(self.ids("Fallbacktoken", "de"), [])

    def test_vendor_does_not_surface_a_tool_missing_the_language(self):
        tool = make_tool(
            "lang-vendor-only",
            vendor="Vendortoken Inc",
            translations={"en": {"name": "English only name"}},
        )
        self.assertIn(tool.pk, self.ids("Vendortoken", "en"))
        self.assertEqual(self.ids("Vendortoken", "de"), [])

    def test_category_does_not_surface_a_tool_missing_the_language(self):
        category = make_category(
            translations={
                "en": {"name": "Cattoken Category", "slug": "cattoken-en"},
                "de": {"name": "Cattoken Kategorie", "slug": "cattoken-de"},
            }
        )
        tool = make_tool(
            "lang-category-only",
            categories=[category],
            translations={"en": {"name": "English only name"}},
        )
        self.assertIn(tool.pk, self.ids("Cattoken", "en"))
        self.assertEqual(self.ids("Cattoken", "de"), [])

    def test_ambient_language_does_not_influence_the_result(self):
        tool = self._bilingual()
        with translation.override("en"):
            german = self.search("Deutschonly", "de")
        self.assertEqual([r.object_id for r in german], [tool.pk])
        self.assertEqual(german[0].title, "Deutschonly Studio")
        self.assertIn("Deutsche Zusammenfassung", german[0].summary)
        self.assertEqual(german[0].url, "/de/catalog/lang-bilingual/")

        with translation.override("de"):
            english = self.search("Englishonly", "en")
        self.assertEqual(english[0].title, "Englishonly Studio")
        self.assertIn("English summary", english[0].summary)
        self.assertEqual(english[0].url, "/en/catalog/lang-bilingual/")

    def test_shared_slug_keeps_one_path_with_two_prefixes(self):
        self._bilingual()
        english = self.search("Englishonly", "en")[0]
        german = self.search("Deutschonly", "de")[0]
        self.assertEqual(english.url, "/en/catalog/lang-bilingual/")
        self.assertEqual(german.url, "/de/catalog/lang-bilingual/")
        self.assertNotEqual(english.title, german.title)


@postgresql_only
class SearchFieldTests(ToolAdapterTestCase):
    def _matched(self, term, tool, language_code="en"):
        return next(
            r for r in self.search(term, language_code) if r.object_id == tool.pk
        ).matched_field

    def test_name_reports_title(self):
        tool = make_tool("field-name", translations={"en": {"name": "Nametoken Studio"}})
        self.assertIs(self._matched("Nametoken", tool), SearchMatchedField.TITLE)

    def test_short_description_reports_summary(self):
        tool = make_tool(
            "field-short",
            translations={
                "en": {"name": "Neutral", "short_description": "Has shorttoken here"}
            },
        )
        self.assertIs(self._matched("shorttoken", tool), SearchMatchedField.SUMMARY)

    def test_long_description_reports_body(self):
        tool = make_tool(
            "field-long",
            translations={
                "en": {
                    "name": "Neutral",
                    "long_description": "<p>Has longtoken here</p>",
                }
            },
        )
        self.assertIs(self._matched("longtoken", tool), SearchMatchedField.BODY)

    def test_vendor_reports_metadata(self):
        tool = make_tool(
            "field-vendor",
            vendor="Vendortoken Inc",
            translations={"en": {"name": "Neutral"}},
        )
        self.assertIs(self._matched("Vendortoken", tool), SearchMatchedField.METADATA)

    def test_category_reports_metadata(self):
        category = make_category(
            translations={"en": {"name": "Categorytoken", "slug": "categorytoken-en"}}
        )
        tool = make_tool(
            "field-category",
            categories=[category],
            translations={"en": {"name": "Neutral"}},
        )
        self.assertIs(self._matched("Categorytoken", tool), SearchMatchedField.METADATA)

    def test_name_wins_when_several_fields_match(self):
        category = make_category(
            translations={"en": {"name": "Everywheretoken", "slug": "everywhere-en"}}
        )
        tool = make_tool(
            "field-all",
            vendor="Everywheretoken Inc",
            categories=[category],
            translations={
                "en": {
                    "name": "Everywheretoken Studio",
                    "short_description": "Everywheretoken summary",
                    "long_description": "Everywheretoken body",
                }
            },
        )
        self.assertIs(self._matched("Everywheretoken", tool), SearchMatchedField.TITLE)

    def test_vendor_outranks_a_long_description_only_hit(self):
        vendor_hit = make_tool(
            "field-rank-vendor",
            vendor="Ranktoken Inc",
            translations={"en": {"name": "Neutral one"}},
        )
        body_hit = make_tool(
            "field-rank-body",
            translations={
                "en": {
                    "name": "Neutral two",
                    "long_description": "<p>Ranktoken in the body.</p>",
                }
            },
        )
        self.assertEqual(self.ids("Ranktoken"), [vendor_hit.pk, body_hit.pk])

    def test_stemmed_hit_is_attributed_correctly(self):
        tool = make_tool(
            "field-stem",
            translations={"de": {"name": "Anleitungen für Anfänger"}},
        )
        self.assertIs(
            self._matched("Anleitung", tool, "de"), SearchMatchedField.TITLE
        )

    def test_excluded_fields_never_match(self):
        category = make_category(
            translations={"en": {"name": "Neutral cat", "slug": "neutral-cat-en"}}
        )
        tool = Tool.objects.create(
            slug="field-excluded",
            website="https://example.test/websitetoken",
            pricing_model="subscription",
            free_tier=True,
            rating=5,
            is_featured=True,
            published_at=timezone.now() - PAST,
        )
        tool.create_translation(
            "en", name="Neutral name", short_description="", long_description=""
        )
        tool.categories.add(category)
        tool.tags.add("tagtoken")
        for term in ("websitetoken", "subscription", "tagtoken"):
            with self.subTest(term=term):
                self.assertNotIn(tool.pk, self.ids(term))


@postgresql_only
class CategoryMetadataTests(ToolAdapterTestCase):
    def _bilingual_category(self, suffix, en_name, de_name):
        return make_category(
            translations={
                "en": {"name": en_name, "slug": f"{suffix}-en"},
                "de": {"name": de_name, "slug": f"{suffix}-de"},
            }
        )

    def test_english_category_name_matches_only_english(self):
        category = self._bilingual_category(
            "cat-lang", "Chatbottoken", "Sprachbottoken"
        )
        tool = make_tool(
            "cat-lang-tool",
            categories=[category],
            translations={"en": {"name": "Neutral"}, "de": {"name": "Neutral"}},
        )
        self.assertIn(tool.pk, self.ids("Chatbottoken", "en"))
        self.assertNotIn(tool.pk, self.ids("Chatbottoken", "de"))

    def test_german_category_name_matches_only_german(self):
        category = self._bilingual_category(
            "cat-lang2", "Chatbottoken", "Sprachbottoken"
        )
        tool = make_tool(
            "cat-lang2-tool",
            categories=[category],
            translations={"en": {"name": "Neutral"}, "de": {"name": "Neutral"}},
        )
        self.assertIn(tool.pk, self.ids("Sprachbottoken", "de"))
        self.assertNotIn(tool.pk, self.ids("Sprachbottoken", "en"))

    def test_category_without_a_translation_in_the_language_does_not_match(self):
        category = make_category(
            translations={"en": {"name": "Untranslatedtoken", "slug": "untrans-en"}}
        )
        tool = make_tool(
            "cat-untranslated",
            categories=[category],
            translations={"en": {"name": "Neutral"}, "de": {"name": "Neutral"}},
        )
        self.assertIn(tool.pk, self.ids("Untranslatedtoken", "en"))
        self.assertNotIn(tool.pk, self.ids("Untranslatedtoken", "de"))

    def test_several_categories_do_not_duplicate_the_tool(self):
        first = self._bilingual_category("cat-dup1", "Multitoken One", "Multitoken Eins")
        second = self._bilingual_category("cat-dup2", "Multitoken Two", "Multitoken Zwei")
        tool = make_tool(
            "cat-duplicate",
            categories=[first, second],
            translations={"en": {"name": "Neutral"}},
        )
        self.assertEqual(self.ids("Multitoken").count(tool.pk), 1)

    def test_two_matching_categories_yield_exactly_one_tool(self):
        first = self._bilingual_category("cat-both1", "Bothtoken Alpha", "Bothtoken Alpha")
        second = self._bilingual_category("cat-both2", "Bothtoken Beta", "Bothtoken Beta")
        tool = make_tool(
            "cat-both",
            categories=[first, second],
            translations={"en": {"name": "Neutral"}},
        )
        self.assertEqual(self.ids("Bothtoken"), [tool.pk])

    def test_future_tool_is_not_found_through_its_category(self):
        category = self._bilingual_category(
            "cat-future", "Scheduledtoken", "Geplanttoken"
        )
        tool = make_tool(
            "cat-future-tool",
            published_at=timezone.now() + FUTURE,
            categories=[category],
            translations={"en": {"name": "Neutral"}},
        )
        self.assertNotIn(tool.pk, self.ids("Scheduledtoken"))

    def test_category_hit_prefers_the_short_description_as_snippet(self):
        category = self._bilingual_category(
            "cat-snippet", "Snippetcattoken", "Snippetcattoken"
        )
        tool = make_tool(
            "cat-snippet-tool",
            categories=[category],
            translations={
                "en": {"name": "Neutral", "short_description": "A helpful summary"}
            },
        )
        result = next(
            r for r in self.search("Snippetcattoken") if r.object_id == tool.pk
        )
        self.assertEqual(result.summary, "A helpful summary")

    def test_category_hit_falls_back_to_the_category_name(self):
        category = self._bilingual_category(
            "cat-nosnippet", "Barecattoken", "Barecattoken"
        )
        tool = make_tool(
            "cat-nosnippet-tool",
            categories=[category],
            translations={"en": {"name": "Neutral"}},
        )
        result = next(r for r in self.search("Barecattoken") if r.object_id == tool.pk)
        self.assertIn("Barecattoken", result.summary)


@postgresql_only
class VendorMetadataTests(ToolAdapterTestCase):
    def test_vendor_hit_prefers_the_short_description_as_snippet(self):
        tool = make_tool(
            "vendor-snippet",
            vendor="Snippetvendortoken",
            translations={
                "en": {"name": "Neutral", "short_description": "A helpful summary"}
            },
        )
        result = next(
            r for r in self.search("Snippetvendortoken") if r.object_id == tool.pk
        )
        self.assertEqual(result.summary, "A helpful summary")

    def test_vendor_hit_falls_back_to_the_vendor_name(self):
        tool = make_tool(
            "vendor-bare",
            vendor="Barevendortoken",
            translations={"en": {"name": "Neutral"}},
        )
        result = next(r for r in self.search("Barevendortoken") if r.object_id == tool.pk)
        self.assertIn("Barevendortoken", result.summary)

    def test_vendor_is_searchable_in_both_languages_when_translations_exist(self):
        tool = make_tool(
            "vendor-bilingual",
            vendor="Bothlangvendortoken",
            translations={"en": {"name": "English"}, "de": {"name": "Deutsch"}},
        )
        self.assertIn(tool.pk, self.ids("Bothlangvendortoken", "en"))
        self.assertIn(tool.pk, self.ids("Bothlangvendortoken", "de"))


@postgresql_only
class FullTextBehaviourTests(ToolAdapterTestCase):
    def _machine_learning(self):
        return make_tool(
            "fts-tool",
            translations={
                "en": {
                    "name": "Machine Learning Studio",
                    "short_description": "An introduction to neural networks",
                    "long_description": "<p>It explains how models are trained.</p>",
                },
                "de": {
                    "name": "Maschinelles Lernen Studio",
                    "short_description": "Eine Einführung in Anleitungen",
                    "long_description": "<p>Es erklärt Übersetzung.</p>",
                },
            },
        )

    def test_and_semantics(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids("machine learning"))
        self.assertNotIn(tool.pk, self.ids("machine bicycle"))

    def test_phrase_search(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids('"Machine Learning"'))
        self.assertNotIn(tool.pk, self.ids('"Learning Machine"'))

    def test_exclusion_operator(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids("machine"))
        self.assertNotIn(tool.pk, self.ids("machine -learning"))

    def test_english_stemming(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids("network"))
        self.assertIn(tool.pk, self.ids("train"))

    def test_german_stemming(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids("Anleitung", "de"))

    def test_case_insensitive(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids("MACHINE"))

    def test_german_stemmer_folds_umlauts(self):
        tool = self._machine_learning()
        self.assertIn(tool.pk, self.ids("Übersetzung", "de"))
        self.assertIn(tool.pk, self.ids("Ubersetzung", "de"))
        self.assertNotIn(tool.pk, self.ids("Uebersetzung", "de"))

    def test_hyphen_and_special_characters_never_raise(self):
        self._machine_learning()
        for term in ("real-time", "a & b", "a | b", "!!!", "a:b", "(a)", '"unclosed'):
            with self.subTest(term=term):
                self.assertIsInstance(self.ids(term), list)

    def test_rank_is_bounded_and_non_negative(self):
        self._machine_learning()
        for result in self.search("machine"):
            self.assertGreaterEqual(result.rank, 0)
            self.assertLess(result.rank, 1)

    def test_name_hit_outranks_a_long_description_only_hit(self):
        name_hit = make_tool(
            "fts-weight-name", translations={"en": {"name": "Weighttoken Studio"}}
        )
        body_hit = make_tool(
            "fts-weight-body",
            translations={
                "en": {
                    "name": "Unrelated",
                    "long_description": "<p>Weighttoken only in the body.</p>",
                }
            },
        )
        self.assertEqual(self.ids("Weighttoken"), [name_hit.pk, body_hit.pk])

    def test_lone_weak_hit_is_not_scaled_up(self):
        strong = make_tool(
            "fts-scale-strong", translations={"en": {"name": "Scaletoken Studio"}}
        )
        weak = make_tool(
            "fts-scale-weak",
            translations={
                "en": {
                    "name": "Unrelated",
                    "long_description": "<p>" + ("filler " * 200) + "Scaletoken</p>",
                }
            },
        )
        self.assertEqual(self.ids("Scaletoken"), [strong.pk, weak.pk])


@postgresql_only
class HtmlBehaviourTests(ToolAdapterTestCase):
    def _with_body(self, slug, body):
        return make_tool(
            slug,
            translations={
                "en": {
                    "name": f"Htmlcase {slug}",
                    "short_description": "",
                    "long_description": body,
                }
            },
        )

    def test_visible_paragraph_matches(self):
        tool = self._with_body("html-visible", "<p>Visible needletoken here</p>")
        self.assertIn(tool.pk, self.ids("needletoken"))

    def test_tag_name_does_not_match(self):
        tool = self._with_body("html-tag", "<needletoken>Visible</needletoken>")
        self.assertNotIn(tool.pk, self.ids("needletoken"))

    def test_class_attribute_does_not_match(self):
        tool = self._with_body("html-class", '<a class="needletoken">Visible</a>')
        self.assertNotIn(tool.pk, self.ids("needletoken"))

    def test_href_attribute_does_not_match(self):
        tool = self._with_body(
            "html-href", '<a href="https://example.test/needletoken">Visible</a>'
        )
        self.assertNotIn(tool.pk, self.ids("needletoken"))

    def test_data_attribute_does_not_match(self):
        tool = self._with_body("html-data", '<div data-x="needletoken">Visible</div>')
        self.assertNotIn(tool.pk, self.ids("needletoken"))

    def test_visible_link_text_matches(self):
        tool = self._with_body(
            "html-linktext", '<a href="https://example.test/">needletoken</a>'
        )
        self.assertIn(tool.pk, self.ids("needletoken"))

    def test_script_and_style_content_do_not_match(self):
        script = self._with_body("html-script", "<script>var needletoken=1;</script>")
        style = self._with_body("html-style", "<style>.needletoken{color:red}</style>")
        self.assertNotIn(script.pk, self.ids("needletoken"))
        self.assertNotIn(style.pk, self.ids("needletoken"))

    def test_tinymce_paragraph_table_and_code_block_match(self):
        paragraph = self._with_body(
            "html-tinymce", "<p>Use <strong>needletoken</strong> today.</p>"
        )
        table = self._with_body(
            "html-table",
            "<table><tbody><tr><td>needletoken</td><td>x</td></tr></tbody></table>",
        )
        code = self._with_body(
            "html-code", '<pre><code class="language-python">needletoken()</code></pre>'
        )
        found = self.ids("needletoken")
        for tool in (paragraph, table, code):
            self.assertIn(tool.pk, found)

    def test_snippet_contains_no_markup_or_attributes(self):
        tool = self._with_body(
            "html-snippet",
            "<p>Visible <strong>needletoken</strong> with "
            '<a href="https://example.test/secret">a link</a>.</p>',
        )
        result = next(r for r in self.search("needletoken") if r.object_id == tool.pk)
        for markup in ("<", ">", "href", "strong", "secret"):
            with self.subTest(markup=markup):
                self.assertNotIn(markup, result.summary)
        self.assertIs(type(result.summary), str)
        self.assertFalse(hasattr(result.summary, "__html__"))


@postgresql_only
class ResultShapeTests(ToolAdapterTestCase):
    def test_returns_a_tuple_of_search_results(self):
        make_tool("shape-tool", translations={"en": {"name": "Shapetoken Studio"}})
        results = self.search("Shapetoken")
        self.assertIsInstance(results, tuple)
        self.assertTrue(all(isinstance(r, SearchResult) for r in results))
        self.assertTrue(all(r.kind is SearchResultKind.TOOL for r in results))

    def test_no_matches_returns_an_empty_tuple(self):
        self.assertEqual(self.search("nothingmatchesthistoken"), ())

    def test_no_duplicates_when_many_sources_match(self):
        category = make_category(
            translations={"en": {"name": "Duplicatetoken cat", "slug": "dup-cat-en"}}
        )
        tool = make_tool(
            "shape-duplicate",
            vendor="Duplicatetoken Inc",
            categories=[category],
            translations={
                "en": {
                    "name": "Duplicatetoken Studio",
                    "short_description": "Duplicatetoken summary",
                    "long_description": "Duplicatetoken body",
                }
            },
        )
        self.assertEqual(self.ids("Duplicatetoken").count(tool.pk), 1)

    def test_results_carry_publication_dates(self):
        tool = make_tool("shape-dates", translations={"en": {"name": "Datetoken Studio"}})
        result = self.search("Datetoken")[0]
        self.assertEqual(result.published_at, tool.published_at)
        self.assertEqual(result.updated_at, tool.updated_at)

    def test_no_result_carries_a_placeholder_or_external_url(self):
        make_tool(
            "shape-url",
            translations={"en": {"name": "Urltoken Studio"}},
        )
        Tool.objects.filter(slug="shape-url").update(
            website="https://example.test/external"
        )
        for result in self.search("Urltoken"):
            self.assertNotEqual(result.url, "#")
            self.assertNotIn("example.test", result.url)
            self.assertTrue(result.url.startswith("/en/catalog/"))

    def test_every_result_url_resolves(self):
        make_tool("shape-reachable", translations={"en": {"name": "Reachabletoken"}})
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        for result in self.search("Reachabletoken"):
            self.assertEqual(self.client.get(result.url).status_code, 200)

    def test_order_is_deterministic(self):
        for index in range(3):
            make_tool(
                f"shape-order-{index}",
                translations={"en": {"name": f"Ordertoken Studio {index}"}},
            )
        self.assertEqual(self.ids("Ordertoken"), self.ids("Ordertoken"))


@postgresql_only
class FailClosedTests(ToolAdapterTestCase):
    def test_unsearchable_query_is_rejected_before_any_database_access(self):
        query = NormalizedSearchQuery(value="a", issue=SearchQueryIssue.TOO_SHORT)
        with self.assertNumQueries(0):
            with self.assertRaises(ValueError):
                self.adapter.search(query=query, language_code="en")

    def test_unsupported_language_is_rejected_before_any_database_access(self):
        from search.fts import UnsupportedSearchLanguage

        with self.assertNumQueries(0):
            with self.assertRaises(UnsupportedSearchLanguage):
                self.adapter.search(
                    query=normalize_search_query("machine"), language_code="fr"
                )

    def test_non_postgresql_backend_fails_closed(self):
        from unittest.mock import patch

        from search.fts import SearchBackendUnavailable

        with patch("search.fts.connection") as fake_connection:
            fake_connection.vendor = "sqlite"
            with self.assertRaises(SearchBackendUnavailable):
                self.adapter.search(
                    query=normalize_search_query("machine"), language_code="en"
                )
