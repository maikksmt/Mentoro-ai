"""
Beta 10.5: guarantees every editorial search adapter must hold.

Each test runs against all four adapters, so a guarantee proven for guides in
Beta 10.4 cannot silently lapse for prompts, use cases or comparisons. Where
the models genuinely differ - use cases and comparisons expose only strictly
published objects - the test branches on the declared semantics rather than
assuming one model's behaviour applies to the rest.

Requires PostgreSQL; the domain layer underneath stays database-independent.
"""
from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from django.utils import translation

from core.models.editorial import EditorialWorkflowMixin
from search.fts import SearchBackendUnavailable, UnsupportedSearchLanguage
from search.query import NormalizedSearchQuery, SearchQueryIssue, normalize_search_query
from search.result_types import SearchMatchedField, SearchResult
from search.tests.editorial_fixtures import (
    ADAPTER_SPECS,
    begin_unpublished_revision,
    edit_without_publishing,
    make_author,
    make_legacy,
    publish,
    republish,
)

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)


class EditorialAdapterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author()

    def search(self, spec, term, language_code="en"):
        return spec.build_adapter().search(
            query=normalize_search_query(term), language_code=language_code
        )

    def ids(self, spec, term, language_code="en"):
        return [result.object_id for result in self.search(spec, term, language_code)]


@postgresql_only
class PublishedContentTests(EditorialAdapterTestCase):
    def test_published_object_is_found_by_its_public_title(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": f"Publicalpha {spec.name}",
                            "intro": "An intro",
                            "body": "A body",
                            "outro": "An outro",
                            "slug": f"pub-title-{spec.name}-en",
                        }
                    },
                )
                results = self.search(spec, "Publicalpha")
                self.assertIn(obj.pk, [r.object_id for r in results])
                result = next(r for r in results if r.object_id == obj.pk)
                self.assertIs(result.kind, spec.kind)
                self.assertEqual(result.title, f"Publicalpha {spec.name}")
                self.assertEqual(result.language_code, "en")
                self.assertEqual(
                    result.url, f"/en/{spec.url_prefix}/pub-title-{spec.name}-en/"
                )

    def test_republishing_swaps_the_searchable_text(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": f"Beforetoken {spec.name}",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"republish-{spec.name}-en",
                        }
                    },
                )
                edit_without_publishing(
                    obj, language_code="en", title=f"Aftertoken {spec.name}"
                )
                self.assertIn(obj.pk, self.ids(spec, "Beforetoken"))
                self.assertNotIn(obj.pk, self.ids(spec, "Aftertoken"))

                obj.move_to_review(by=self.author)
                obj.last_published_revision_id = 1
                obj.save()
                republish(obj, author=self.author)

                self.assertIn(obj.pk, self.ids(spec, "Aftertoken"))
                self.assertNotIn(obj.pk, self.ids(spec, "Beforetoken"))


@postgresql_only
class DraftSafetyTests(EditorialAdapterTestCase):
    """No unpublished change may become searchable or reach a result."""

    def _published(self, spec, slug, **values):
        payload = {
            "title": f"Basetoken {spec.name}",
            "intro": "published intro",
            "body": "published body",
            "outro": "published outro",
            "slug": slug,
        }
        payload.update(values)
        return publish(spec, author=self.author, translations={"en": payload})

    def test_draft_title_is_not_searchable(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._published(spec, f"draft-title-{spec.name}-en")
                edit_without_publishing(
                    obj, language_code="en", title="Draftneedle title"
                )
                self.assertNotIn(obj.pk, self.ids(spec, "Draftneedle"))
                self.assertIn(obj.pk, self.ids(spec, "Basetoken"))
                result = next(
                    r for r in self.search(spec, "Basetoken") if r.object_id == obj.pk
                )
                self.assertNotIn("Draftneedle", result.title)

    def test_draft_text_fields_are_not_searchable(self):
        for spec in ADAPTER_SPECS:
            for name in spec.text_fields:
                with self.subTest(adapter=spec.name, field=name):
                    obj = self._published(spec, f"draft-{name}-{spec.name}-en")
                    edit_without_publishing(
                        obj, language_code="en", **{name: "Draftneedle text"}
                    )
                    self.assertNotIn(obj.pk, self.ids(spec, "Draftneedle"))
                    self.assertIn(obj.pk, self.ids(spec, "Basetoken"))

    def test_draft_slug_never_reaches_a_result_url(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._published(spec, f"public-slug-{spec.name}-en")
                edit_without_publishing(
                    obj, language_code="en", slug=f"draftneedle-{spec.name}-en"
                )
                result = next(
                    r for r in self.search(spec, "Basetoken") if r.object_id == obj.pk
                )
                self.assertIn(f"public-slug-{spec.name}-en", result.url)
                self.assertNotIn("draftneedle", result.url)

    def test_draft_filling_an_empty_published_field_is_not_searchable(self):
        for spec in ADAPTER_SPECS:
            for name in spec.text_fields:
                with self.subTest(adapter=spec.name, field=name):
                    obj = self._published(
                        spec, f"empty-{name}-{spec.name}-en", **{name: ""}
                    )
                    edit_without_publishing(
                        obj, language_code="en", **{name: "Draftneedle filled"}
                    )
                    self.assertNotIn(obj.pk, self.ids(spec, "Draftneedle"))

    def test_result_summary_never_carries_a_draft_value(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                empty = {name: "" for name in spec.text_fields}
                obj = self._published(spec, f"empty-all-{spec.name}-en", **empty)
                edit_without_publishing(
                    obj,
                    language_code="en",
                    **{name: "Draftneedle text" for name in spec.text_fields},
                )
                result = next(
                    r for r in self.search(spec, "Basetoken") if r.object_id == obj.pk
                )
                self.assertEqual(result.summary, "")

    def test_language_added_after_publish_is_not_searchable(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._published(spec, f"late-de-{spec.name}-en")
                payload = {
                    "title": "Draftneedle Titel",
                    "slug": f"draftneedle-late-{spec.name}-de",
                    **{name: "Draftneedle Text" for name in spec.text_fields},
                    **spec.required_extra,
                }
                obj.create_translation("de", **payload)

                self.assertEqual(self.ids(spec, "Draftneedle", "de"), [])
                self.assertEqual(self.ids(spec, "Basetoken", "de"), [])
                self.assertIn(obj.pk, self.ids(spec, "Basetoken", "en"))

    def test_mirrored_language_added_after_publish_is_not_searchable(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "de": {
                            "title": f"Deutschbasis {spec.name}",
                            "intro": "Deutsches Intro",
                            "body": "Deutscher Body",
                            "outro": "Deutsches Outro",
                            "slug": f"late-en-{spec.name}-de",
                        }
                    },
                )
                payload = {
                    "title": "Draftneedle title",
                    "slug": f"draftneedle-late-{spec.name}-en",
                    **{name: "Draftneedle text" for name in spec.text_fields},
                    **spec.required_extra,
                }
                obj.create_translation("en", **payload)

                self.assertEqual(self.ids(spec, "Draftneedle", "en"), [])
                self.assertIn(obj.pk, self.ids(spec, "Deutschbasis", "de"))


@postgresql_only
class LegacyRecordTests(EditorialAdapterTestCase):
    def test_record_without_any_snapshot_stays_searchable(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = make_legacy(
                    spec,
                    translations={
                        "en": {
                            "title": f"Legacytoken {spec.name}",
                            "intro": "legacy intro",
                            "body": "legacy body",
                            "outro": "legacy outro",
                            "slug": f"legacy-{spec.name}-en",
                        }
                    },
                )
                self.assertEqual(obj.live_i18n, {})
                self.assertIn(obj.pk, self.ids(spec, "Legacytoken"))

    def test_record_without_snapshot_has_no_cross_language_fallback(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = make_legacy(
                    spec,
                    translations={
                        "en": {
                            "title": f"Legacycross {spec.name}",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"legacy-cross-{spec.name}-en",
                        }
                    },
                )
                self.assertIn(obj.pk, self.ids(spec, "Legacycross", "en"))
                self.assertEqual(self.ids(spec, "Legacycross", "de"), [])


@postgresql_only
class VisibilitySemanticsTests(EditorialAdapterTestCase):
    """Search mirrors each model's own public queryset - never its own rule."""

    def test_draft_is_never_found(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = spec.model.objects.create(
                    status=EditorialWorkflowMixin.STATUS_DRAFT
                )
                obj.create_translation(
                    "en",
                    title=f"Draftstatus {spec.name}",
                    slug=f"draft-status-{spec.name}-en",
                    **{name: "x" for name in spec.text_fields},
                    **spec.required_extra,
                )
                self.assertNotIn(obj.pk, self.ids(spec, "Draftstatus"))

    def test_review_without_live_revision_is_never_found(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = spec.model.objects.create(
                    status=EditorialWorkflowMixin.STATUS_REVIEW
                )
                obj.create_translation(
                    "en",
                    title=f"Reviewstatus {spec.name}",
                    slug=f"review-status-{spec.name}-en",
                    **{name: "x" for name in spec.text_fields},
                    **spec.required_extra,
                )
                self.assertNotIn(obj.pk, self.ids(spec, "Reviewstatus"))

    def test_review_with_live_revision_follows_the_models_own_rule(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": f"Livereview {spec.name}",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"live-review-{spec.name}-en",
                        }
                    },
                )
                begin_unpublished_revision(
                    obj, author=self.author, language_code="en", intro="edited"
                )
                public_ids = list(
                    spec.model.objects.visible_in_language("en").values_list(
                        "pk", flat=True
                    )
                )
                found = obj.pk in self.ids(spec, "Livereview")
                self.assertEqual(
                    found,
                    obj.pk in public_ids,
                    "search visibility must equal the model's public queryset",
                )
                self.assertEqual(found, spec.review_with_live_revision_is_public)


@postgresql_only
class LanguageIsolationTests(EditorialAdapterTestCase):
    def _bilingual(self, spec):
        return publish(
            spec,
            author=self.author,
            translations={
                "en": {
                    "title": f"Englishonly {spec.name}",
                    "intro": "English intro",
                    "body": "English body",
                    "outro": "English outro",
                    "slug": f"bilingual-{spec.name}-en",
                },
                "de": {
                    "title": f"Deutschonly {spec.name}",
                    "intro": "Deutsches Intro",
                    "body": "Deutscher Body",
                    "outro": "Deutsches Outro",
                    "slug": f"bilingual-{spec.name}-de",
                },
            },
        )

    def test_english_token_does_not_match_german_search(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._bilingual(spec)
                self.assertIn(obj.pk, self.ids(spec, "Englishonly", "en"))
                self.assertNotIn(obj.pk, self.ids(spec, "Englishonly", "de"))

    def test_german_token_does_not_match_english_search(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._bilingual(spec)
                self.assertIn(obj.pk, self.ids(spec, "Deutschonly", "de"))
                self.assertNotIn(obj.pk, self.ids(spec, "Deutschonly", "en"))

    def test_missing_translation_yields_no_result(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": f"Fallbacktoken {spec.name}",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"fallback-{spec.name}-en",
                        }
                    },
                )
                self.assertIn(obj.pk, self.ids(spec, "Fallbacktoken", "en"))
                self.assertEqual(self.ids(spec, "Fallbacktoken", "de"), [])

    def test_ambient_language_does_not_influence_the_result(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._bilingual(spec)
                with translation.override("en"):
                    german = self.search(spec, "Deutschonly", "de")
                self.assertEqual([r.object_id for r in german], [obj.pk])
                self.assertEqual(german[0].title, f"Deutschonly {spec.name}")
                self.assertEqual(
                    german[0].url, f"/de/{spec.url_prefix}/bilingual-{spec.name}-de/"
                )

                with translation.override("de"):
                    english = self.search(spec, "Englishonly", "en")
                self.assertEqual(english[0].title, f"Englishonly {spec.name}")
                self.assertEqual(
                    english[0].url, f"/en/{spec.url_prefix}/bilingual-{spec.name}-en/"
                )

    def test_title_summary_and_url_share_one_language(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                self._bilingual(spec)
                result = self.search(spec, "Deutschonly", "de")[0]
                self.assertEqual(result.title, f"Deutschonly {spec.name}")
                self.assertIn("Deutsches Intro", result.summary)
                self.assertIn(f"/de/{spec.url_prefix}/", result.url)
                self.assertEqual(result.language_code, "de")


@postgresql_only
class FullTextBehaviourTests(EditorialAdapterTestCase):
    def _machine_learning(self, spec):
        return publish(
            spec,
            author=self.author,
            translations={
                "en": {
                    "title": "Machine Learning Basics",
                    "intro": "An introduction to neural networks",
                    "body": "<p>This explains how models are trained.</p>",
                    "outro": "A closing note",
                    "slug": f"fts-{spec.name}-en",
                },
                "de": {
                    "title": "Maschinelles Lernen Grundlagen",
                    "intro": "Eine Einführung in Anleitungen",
                    "body": "<p>Dieser Text erklärt Übersetzung.</p>",
                    "outro": "Ein Schlusswort",
                    "slug": f"fts-{spec.name}-de",
                },
            },
        )

    def test_and_semantics(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, "machine learning"))
                self.assertNotIn(obj.pk, self.ids(spec, "machine bicycle"))

    def test_phrase_search(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, '"Machine Learning"'))
                self.assertNotIn(obj.pk, self.ids(spec, '"Learning Machine"'))

    def test_exclusion_operator(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, "machine"))
                self.assertNotIn(obj.pk, self.ids(spec, "machine -learning"))

    def test_english_stemming(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, "network"))
                self.assertIn(obj.pk, self.ids(spec, "train"))

    def test_german_stemming(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, "Anleitung", "de"))
                self.assertIn(obj.pk, self.ids(spec, "Grundlage", "de"))

    def test_case_insensitive(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, "MACHINE"))

    def test_german_stemmer_folds_umlauts(self):
        # Confirmed empirically in Beta 10.4: the German Snowball stemmer
        # folds umlauts, so the unaccent extension is not needed for them.
        # The ue/ae/oe digraph spelling is a different string and does not.
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._machine_learning(spec)
                self.assertIn(obj.pk, self.ids(spec, "Übersetzung", "de"))
                self.assertIn(obj.pk, self.ids(spec, "Ubersetzung", "de"))
                self.assertNotIn(obj.pk, self.ids(spec, "Uebersetzung", "de"))

    def test_hyphen_and_special_characters_never_raise(self):
        for spec in ADAPTER_SPECS:
            self._machine_learning(spec)
            for term in ("real-time", "a & b", "a | b", "!!!", "a:b", "(a)", '"unclosed'):
                with self.subTest(adapter=spec.name, term=term):
                    self.assertIsInstance(self.ids(spec, term), list)

    def test_rank_is_bounded_and_non_negative(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                self._machine_learning(spec)
                for result in self.search(spec, "machine"):
                    self.assertGreaterEqual(result.rank, 0)
                    self.assertLess(result.rank, 1)

    def test_title_hit_outranks_a_body_only_hit(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                title_hit = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Weighttoken in the title",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"weight-title-{spec.name}-en",
                        }
                    },
                )
                body_hit = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Unrelated heading",
                            "intro": "unrelated intro",
                            "body": "<p>Weighttoken only in the body.</p>",
                            "outro": "unrelated outro",
                            "slug": f"weight-body-{spec.name}-en",
                        }
                    },
                )
                self.assertEqual(
                    self.ids(spec, "Weighttoken"), [title_hit.pk, body_hit.pk]
                )

    def test_lone_weak_hit_is_not_scaled_up(self):
        # If an adapter rescaled against its own maximum, the single weak hit
        # would become 1.0 and lead. Ranks stay raw and comparable.
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                strong = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Scaletoken heading",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"scale-strong-{spec.name}-en",
                        }
                    },
                )
                weak = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Unrelated",
                            "intro": "i",
                            "body": "<p>" + ("filler " * 200) + "Scaletoken</p>",
                            "outro": "o",
                            "slug": f"scale-weak-{spec.name}-en",
                        }
                    },
                )
                ordered = self.ids(spec, "Scaletoken")
                self.assertEqual(ordered, [strong.pk, weak.pk])


@postgresql_only
class HtmlBehaviourTests(EditorialAdapterTestCase):
    """
    PostgreSQL's parser skips tags, attributes and script/style content; that
    is pinned exhaustively for guides in Beta 10.4. Here each adapter gets one
    visible-text case and one attribute negative case, so the shared behaviour
    is confirmed per model without duplicating the whole matrix.
    """

    def _with_body(self, spec, slug, body):
        return publish(
            spec,
            author=self.author,
            translations={
                "en": {
                    "title": f"Htmlcase {spec.name}",
                    "intro": "neutral intro",
                    "body": body,
                    "outro": "neutral outro",
                    "slug": slug,
                }
            },
        )

    def test_visible_richtext_matches(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._with_body(
                    spec,
                    f"html-visible-{spec.name}-en",
                    "<p>Learn how to use <strong>needletoken</strong> today.</p>",
                )
                self.assertIn(obj.pk, self.ids(spec, "needletoken"))

    def test_attribute_only_token_does_not_match(self):
        for spec in ADAPTER_SPECS:
            for markup in (
                '<a class="needletoken">Visible text</a>',
                '<a href="https://example.test/needletoken">Visible text</a>',
                '<div data-tag="needletoken">Visible text</div>',
            ):
                with self.subTest(adapter=spec.name, markup=markup[:24]):
                    obj = self._with_body(
                        spec,
                        f"html-attr-{spec.name}-{abs(hash(markup)) % 10000}-en",
                        markup,
                    )
                    self.assertNotIn(obj.pk, self.ids(spec, "needletoken"))

    def test_snippet_contains_no_markup(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = self._with_body(
                    spec,
                    f"html-snippet-{spec.name}-en",
                    "<p>Visible <strong>needletoken</strong> with "
                    '<a href="https://example.test/secret">a link</a>.</p>',
                )
                result = next(
                    r for r in self.search(spec, "needletoken") if r.object_id == obj.pk
                )
                for markup in ("<", ">", "href", "strong", "secret"):
                    self.assertNotIn(markup, result.summary)
                self.assertIs(type(result.summary), str)
                self.assertFalse(hasattr(result.summary, "__html__"))


@postgresql_only
class MatchedFieldTests(EditorialAdapterTestCase):
    def _matched(self, spec, term, obj):
        return next(
            r for r in self.search(spec, term) if r.object_id == obj.pk
        ).matched_field

    def test_title_match(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": f"Titletoken {spec.name}",
                            "intro": "neutral",
                            "body": "neutral",
                            "outro": "neutral",
                            "slug": f"mf-title-{spec.name}-en",
                        }
                    },
                )
                self.assertIs(
                    self._matched(spec, "Titletoken", obj), SearchMatchedField.TITLE
                )

    def test_intro_match_is_reported_as_summary(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Neutral heading",
                            "intro": f"Contains introtoken {spec.name}",
                            "body": "neutral",
                            "outro": "neutral",
                            "slug": f"mf-intro-{spec.name}-en",
                        }
                    },
                )
                self.assertIs(
                    self._matched(spec, "introtoken", obj), SearchMatchedField.SUMMARY
                )

    def test_body_match(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Neutral heading",
                            "intro": "neutral intro",
                            "body": f"<p>Contains bodytoken {spec.name}</p>",
                            "outro": "neutral",
                            "slug": f"mf-body-{spec.name}-en",
                        }
                    },
                )
                self.assertIs(
                    self._matched(spec, "bodytoken", obj), SearchMatchedField.BODY
                )

    def test_title_wins_when_several_fields_match(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Everywheretoken heading",
                            "intro": "Everywheretoken intro",
                            "body": "<p>Everywheretoken body</p>",
                            "outro": "Everywheretoken outro",
                            "slug": f"mf-all-{spec.name}-en",
                        }
                    },
                )
                self.assertIs(
                    self._matched(spec, "Everywheretoken", obj),
                    SearchMatchedField.TITLE,
                )

    def test_stemmed_hit_is_attributed_correctly(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "de": {
                            "title": f"Anleitungen für Anfänger {spec.name}",
                            "intro": "neutral",
                            "body": "neutral",
                            "outro": "neutral",
                            "slug": f"mf-stem-{spec.name}-de",
                        }
                    },
                )
                result = next(
                    r
                    for r in self.search(spec, "Anleitung", "de")
                    if r.object_id == obj.pk
                )
                self.assertIs(result.matched_field, SearchMatchedField.TITLE)


@postgresql_only
class ResultShapeTests(EditorialAdapterTestCase):
    def test_no_duplicates_when_many_fields_match(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Duplicatetoken heading",
                            "intro": "Duplicatetoken intro",
                            "body": "<p>Duplicatetoken body</p>",
                            "outro": "Duplicatetoken outro",
                            "slug": f"dup-{spec.name}-en",
                        }
                    },
                )
                self.assertEqual(self.ids(spec, "Duplicatetoken").count(obj.pk), 1)

    def test_results_are_a_tuple_of_search_results(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Shapetoken heading",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"shape-{spec.name}-en",
                        }
                    },
                )
                results = self.search(spec, "Shapetoken")
                self.assertIsInstance(results, tuple)
                self.assertTrue(all(isinstance(r, SearchResult) for r in results))
                self.assertTrue(all(r.kind is spec.kind for r in results))

    def test_no_result_carries_a_placeholder_url(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Urltoken heading",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"url-{spec.name}-en",
                        }
                    },
                )
                for result in self.search(spec, "Urltoken"):
                    self.assertNotEqual(result.url, "#")

    def test_results_carry_publication_dates(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                obj = publish(
                    spec,
                    author=self.author,
                    translations={
                        "en": {
                            "title": "Datetoken heading",
                            "intro": "i",
                            "body": "b",
                            "outro": "o",
                            "slug": f"date-{spec.name}-en",
                        }
                    },
                )
                result = self.search(spec, "Datetoken")[0]
                self.assertEqual(result.published_at, obj.published_at)
                self.assertEqual(result.updated_at, obj.updated_at)

    def test_no_matches_returns_an_empty_tuple(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                self.assertEqual(self.search(spec, "nothingmatchesthistoken"), ())

    def test_order_is_deterministic(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                for index in range(3):
                    publish(
                        spec,
                        author=self.author,
                        translations={
                            "en": {
                                "title": f"Ordertoken heading {index}",
                                "intro": "i",
                                "body": "b",
                                "outro": "o",
                                "slug": f"order-{spec.name}-{index}-en",
                            }
                        },
                    )
                self.assertEqual(
                    self.ids(spec, "Ordertoken"), self.ids(spec, "Ordertoken")
                )


@postgresql_only
class AdapterContractTests(EditorialAdapterTestCase):
    def test_every_adapter_declares_its_kind(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                self.assertIs(spec.build_adapter().kind, spec.kind)

    def test_kinds_are_distinct(self):
        kinds = [spec.kind for spec in ADAPTER_SPECS]
        self.assertEqual(len(kinds), len(set(kinds)))

    def test_search_is_keyword_only(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                with self.assertRaises(TypeError):
                    spec.build_adapter().search(normalize_search_query("x y"), "en")

    def test_unsearchable_query_is_rejected_before_any_database_access(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                query = NormalizedSearchQuery(value="a", issue=SearchQueryIssue.TOO_SHORT)
                with self.assertNumQueries(0):
                    with self.assertRaises(ValueError):
                        spec.build_adapter().search(query=query, language_code="en")

    def test_unsupported_language_is_rejected_before_any_database_access(self):
        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                with self.assertNumQueries(0):
                    with self.assertRaises(UnsupportedSearchLanguage):
                        spec.build_adapter().search(
                            query=normalize_search_query("machine"),
                            language_code="fr",
                        )

    def test_non_postgresql_backend_fails_closed(self):
        from unittest.mock import patch

        for spec in ADAPTER_SPECS:
            with self.subTest(adapter=spec.name):
                with patch("search.fts.connection") as fake_connection:
                    fake_connection.vendor = "sqlite"
                    with self.assertRaises(SearchBackendUnavailable):
                        spec.build_adapter().search(
                            query=normalize_search_query("machine"),
                            language_code="en",
                        )
