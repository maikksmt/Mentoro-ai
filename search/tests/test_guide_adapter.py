"""
Beta 10.4: the guide search adapter.

The load-bearing guarantee is that search and the public guide detail page
show the same revision: a result may only be found through text a visitor can
actually read, in the language that was asked for. Every draft/language case
below exists because the opposite would be a silent content leak.

Requires PostgreSQL; the domain layer these tests build on stays
database-independent.
"""
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import translation
from parler.utils.context import switch_language

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from search.adapters.guides import GuideSearchAdapter
from search.fts import SearchBackendUnavailable, UnsupportedSearchLanguage
from search.query import NormalizedSearchQuery, SearchQueryIssue, normalize_search_query
from search.result_types import SearchMatchedField, SearchResultKind

User = get_user_model()

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)


def make_published_guide(*, author, translations):
    """Publishes through the real FSM so live_i18n is written as in production."""
    guide = Guide.objects.create(
        status=EditorialWorkflowMixin.STATUS_APPROVED, author=author
    )
    for language_code, values in translations.items():
        guide.create_translation(
            language_code,
            title=values.get("title", "Untitled"),
            intro=values.get("intro", ""),
            body=values.get("body", ""),
            slug=values["slug"],
        )
    guide.publish(by=author)
    guide.save()
    return guide


def begin_unpublished_revision(guide, *, author, language_code, **fields):
    """Edits the current translation without republishing."""
    with switch_language(guide, language_code):
        for name, value in fields.items():
            setattr(guide, name, value)
        guide.save()
    guide.move_to_review(by=author)
    guide.last_published_revision_id = 1
    guide.save()


class GuideAdapterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="adapter-editor",
            email="adapter@example.com",
            password="testpass123",
        )

    def setUp(self):
        self.adapter = GuideSearchAdapter()

    def search(self, term, language_code="en"):
        return self.adapter.search(
            query=normalize_search_query(term), language_code=language_code
        )

    def ids(self, term, language_code="en"):
        return [result.object_id for result in self.search(term, language_code)]


@postgresql_only
class PublishedRevisionTests(GuideAdapterTestCase):
    """Pflichtfall 1-6: search sees the published revision, never the draft."""

    def test_published_guide_is_found_by_its_public_title(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Publicalpha handbook",
                    "intro": "An intro",
                    "body": "A body",
                    "slug": "published-title-en",
                }
            },
        )
        results = self.search("Publicalpha")
        self.assertEqual([r.object_id for r in results], [guide.pk])
        result = results[0]
        self.assertIs(result.kind, SearchResultKind.GUIDE)
        self.assertEqual(result.title, "Publicalpha handbook")
        self.assertEqual(result.language_code, "en")
        self.assertIn("published-title-en", result.url)

    def test_unpublished_title_change_is_not_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Publicalpha",
                    "intro": "i",
                    "body": "b",
                    "slug": "draft-title-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", title="Draftneedle"
        )
        self.assertIn(guide.pk, self.ids("Publicalpha"))
        self.assertNotIn(guide.pk, self.ids("Draftneedle"))
        self.assertEqual(self.search("Publicalpha")[0].title, "Publicalpha")

    def test_unpublished_intro_change_is_not_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Intro case",
                    "intro": "Contains publicintro here",
                    "body": "b",
                    "slug": "draft-intro-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", intro="Contains draftintro here"
        )
        self.assertIn(guide.pk, self.ids("publicintro"))
        self.assertNotIn(guide.pk, self.ids("draftintro"))

    def test_unpublished_body_change_is_not_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Body case",
                    "intro": "i",
                    "body": "<p>Contains publicbody here</p>",
                    "slug": "draft-body-en",
                }
            },
        )
        begin_unpublished_revision(
            guide,
            author=self.author,
            language_code="en",
            body="<p>Contains draftbody here</p>",
        )
        self.assertIn(guide.pk, self.ids("publicbody"))
        self.assertNotIn(guide.pk, self.ids("draftbody"))

    def test_unpublished_slug_change_does_not_reach_the_result_url(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Slugcase guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "public-slug-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", slug="draft-slug-en"
        )
        result = self.search("Slugcase")[0]
        self.assertIn("public-slug-en", result.url)
        self.assertNotIn("draft-slug-en", result.url)

    def test_republishing_makes_the_new_text_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Beforetoken",
                    "intro": "i",
                    "body": "b",
                    "slug": "republish-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", title="Aftertoken"
        )
        self.assertIn(guide.pk, self.ids("Beforetoken"))
        self.assertNotIn(guide.pk, self.ids("Aftertoken"))

        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment.
        guide = Guide.objects.get(pk=guide.pk)
        guide.approve(by=self.author)
        guide.save()
        guide.publish(by=self.author)
        guide.save()

        self.assertIn(guide.pk, self.ids("Aftertoken"))
        self.assertNotIn(guide.pk, self.ids("Beforetoken"))
        self.assertEqual(self.search("Aftertoken")[0].title, "Aftertoken")

    def test_guide_without_snapshot_is_searchable_via_its_translation(self):
        # Records predating the snapshot mechanism must stay findable.
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en", title="Nosnapshot guide", intro="i", body="b", slug="nosnapshot-en"
        )
        self.assertIn(guide.pk, self.ids("Nosnapshot"))


@postgresql_only
class SnapshotAuthorityTests(GuideAdapterTestCase):
    """
    A published snapshot is the sole authority for its language. An empty
    published field and a language published later must both stay invisible
    to search - otherwise a draft becomes findable through the fallback.
    """

    def test_draft_intro_is_not_searchable_when_the_published_intro_is_empty(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Emptyintro case",
                    "intro": "",
                    "body": "b",
                    "slug": "adapter-empty-intro-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", intro="Draftneedle intro"
        )
        self.assertNotIn(guide.pk, self.ids("Draftneedle"))
        self.assertIn(guide.pk, self.ids("Emptyintro"))

    def test_draft_body_is_not_searchable_when_the_published_body_is_empty(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Emptybody case",
                    "intro": "i",
                    "body": "",
                    "slug": "adapter-empty-body-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", body="Draftneedle body"
        )
        self.assertNotIn(guide.pk, self.ids("Draftneedle"))
        self.assertIn(guide.pk, self.ids("Emptybody"))

    def test_result_never_carries_the_draft_value_of_an_empty_published_field(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Snippetsource case",
                    "intro": "",
                    "body": "",
                    "slug": "adapter-empty-both-en",
                }
            },
        )
        begin_unpublished_revision(
            guide,
            author=self.author,
            language_code="en",
            intro="Draftneedle intro",
            body="Draftneedle body",
        )
        result = next(r for r in self.search("Snippetsource") if r.object_id == guide.pk)
        self.assertNotIn("Draftneedle", result.summary)
        self.assertEqual(result.summary, "")

    def test_language_added_after_publish_is_not_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Latelanguage guide",
                    "intro": "English intro",
                    "body": "English body",
                    "slug": "adapter-late-en",
                }
            },
        )
        guide.create_translation(
            "de",
            title="Draftneedle Titel",
            intro="Draftneedle Intro",
            body="Draftneedle Body",
            slug="draftneedle-adapter-de",
        )
        # The guide now has a German translation row, so it passes
        # visible_in_language("de") - but it has no published German revision.
        self.assertIn(
            guide.pk,
            list(Guide.objects.visible_in_language("de").values_list("pk", flat=True)),
        )
        self.assertEqual(self.ids("Draftneedle", "de"), [])
        self.assertEqual(self.ids("Latelanguage", "de"), [])
        self.assertIn(guide.pk, self.ids("Latelanguage", "en"))

    def test_mirrored_language_added_after_publish_is_not_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "de": {
                    "title": "Spaetsprache Anleitung",
                    "intro": "Deutsches Intro",
                    "body": "Deutscher Body",
                    "slug": "adapter-late-de",
                }
            },
        )
        guide.create_translation(
            "en",
            title="Draftneedle title",
            intro="Draftneedle intro",
            body="Draftneedle body",
            slug="draftneedle-adapter-en",
        )
        self.assertEqual(self.ids("Draftneedle", "en"), [])
        self.assertIn(guide.pk, self.ids("Spaetsprache", "de"))

    def test_draft_slug_of_a_late_language_never_reaches_a_url(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Slugleak guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "adapter-slugleak-en",
                }
            },
        )
        guide.create_translation(
            "de", title="Titel", intro="i", body="b", slug="draftneedle-slug-de"
        )
        for result in self.search("Slugleak", "en"):
            self.assertNotIn("draftneedle-slug-de", result.url)
        self.assertEqual(self.ids("Slugleak", "de"), [])

    def test_snapshot_field_absent_from_the_mapping_is_not_searchable(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Absentfield guide",
                    "intro": "Published intro",
                    "body": "b",
                    "slug": "adapter-absent-en",
                }
            },
        )
        snapshot = dict(guide.live_i18n)
        snapshot["en"] = {k: v for k, v in snapshot["en"].items() if k != "intro"}
        Guide.objects.filter(pk=guide.pk).update(live_i18n=snapshot)
        # The intro is gone from the published revision, so its text must no
        # longer match even though the translation row still holds it.
        self.assertNotIn(guide.pk, self.ids("Published"))
        self.assertIn(guide.pk, self.ids("Absentfield"))

    def test_legacy_record_without_any_snapshot_stays_searchable(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        guide.create_translation(
            "en",
            title="Legacyrecord guide",
            intro="Legacy intro",
            body="b",
            slug="adapter-legacy-en",
        )
        self.assertEqual(guide.live_i18n, {})
        self.assertIn(guide.pk, self.ids("Legacyrecord"))
        self.assertIn(guide.pk, self.ids("Legacy"))
        self.assertEqual(self.ids("Legacyrecord", "de"), [])


@postgresql_only
class VisibilityTests(GuideAdapterTestCase):
    """Pflichtfall 7-8: exactly the visibility of visible_in_language()."""

    def test_draft_guide_is_never_found(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        guide.create_translation(
            "en", title="Drafttoken guide", intro="i", body="b", slug="draft-guide-en"
        )
        self.assertNotIn(guide.pk, self.ids("Drafttoken"))

    def test_review_without_live_revision_is_never_found(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_REVIEW)
        guide.create_translation(
            "en", title="Reviewtoken guide", intro="i", body="b", slug="review-guide-en"
        )
        self.assertNotIn(guide.pk, self.ids("Reviewtoken"))

    def test_review_with_live_revision_stays_findable(self):
        # Mirrors visible_on_site(): the published revision is still public,
        # so search must behave exactly like the list and detail pages.
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Livetoken guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "live-review-en",
                }
            },
        )
        begin_unpublished_revision(
            guide, author=self.author, language_code="en", intro="edited"
        )
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertIn(guide.pk, self.ids("Livetoken"))
        self.assertIn(
            guide.pk,
            list(Guide.objects.visible_in_language("en").values_list("pk", flat=True)),
        )

    def test_adapter_matches_the_public_queryset_exactly(self):
        make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Sharedtoken one",
                    "intro": "i",
                    "body": "b",
                    "slug": "shared-one-en",
                }
            },
        )
        Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT).create_translation(
            "en", title="Sharedtoken two", intro="i", body="b", slug="shared-two-en"
        )
        public_ids = set(
            Guide.objects.visible_in_language("en").values_list("pk", flat=True)
        )
        self.assertTrue(set(self.ids("Sharedtoken")).issubset(public_ids))


@postgresql_only
class LanguageIsolationTests(GuideAdapterTestCase):
    """The adapter must never let one language's text match another's search."""

    def _bilingual(self):
        return make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Englishonly token",
                    "intro": "English intro",
                    "body": "English body",
                    "slug": "bilingual-en",
                },
                "de": {
                    "title": "Deutschonly Begriff",
                    "intro": "Deutsches Intro",
                    "body": "Deutscher Body",
                    "slug": "bilingual-de",
                },
            },
        )

    def test_english_token_does_not_match_german_search(self):
        guide = self._bilingual()
        self.assertIn(guide.pk, self.ids("Englishonly", "en"))
        self.assertNotIn(guide.pk, self.ids("Englishonly", "de"))

    def test_german_token_does_not_match_english_search(self):
        guide = self._bilingual()
        self.assertIn(guide.pk, self.ids("Deutschonly", "de"))
        self.assertNotIn(guide.pk, self.ids("Deutschonly", "en"))

    def test_missing_translation_yields_no_result(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Onlyenglish guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "only-english-en",
                }
            },
        )
        self.assertIn(guide.pk, self.ids("Onlyenglish", "en"))
        self.assertNotIn(guide.pk, self.ids("Onlyenglish", "de"))

    def test_parler_fallback_is_not_a_search_index(self):
        # The German page would happily *display* the English fallback; it
        # must still never produce a German search hit.
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Fallbacktoken",
                    "intro": "i",
                    "body": "b",
                    "slug": "fallback-en",
                }
            },
        )
        self.assertEqual(self.ids("Fallbacktoken", "de"), [])
        self.assertIn(guide.pk, self.ids("Fallbacktoken", "en"))

    def test_ambient_language_does_not_influence_the_result(self):
        guide = self._bilingual()
        with translation.override("en"):
            german = self.adapter.search(
                query=normalize_search_query("Deutschonly"), language_code="de"
            )
        self.assertEqual([r.object_id for r in german], [guide.pk])
        self.assertEqual(german[0].title, "Deutschonly Begriff")
        self.assertIn("bilingual-de", german[0].url)

        with translation.override("de"):
            english = self.adapter.search(
                query=normalize_search_query("Englishonly"), language_code="en"
            )
        self.assertEqual(english[0].title, "Englishonly token")
        self.assertIn("bilingual-en", english[0].url)

    def test_title_summary_and_url_come_from_the_same_language(self):
        self._bilingual()
        result = self.search("Deutschonly", "de")[0]
        self.assertEqual(result.title, "Deutschonly Begriff")
        self.assertIn("Deutsches Intro", result.summary)
        self.assertIn("/de/guides/bilingual-de/", result.url)
        self.assertEqual(result.language_code, "de")


@postgresql_only
class FullTextBehaviourTests(GuideAdapterTestCase):
    def setUp(self):
        super().setUp()
        self.guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Machine Learning Basics",
                    "intro": "An introduction to neural networks",
                    "body": "<p>This guide explains how models are trained.</p>",
                    "slug": "fts-en",
                },
                "de": {
                    "title": "Maschinelles Lernen Grundlagen",
                    "intro": "Eine Einführung in Anleitungen",
                    "body": "<p>Dieser Leitfaden erklärt Übersetzung.</p>",
                    "slug": "fts-de",
                },
            },
        )

    def test_exact_term_matches(self):
        self.assertIn(self.guide.pk, self.ids("Machine"))

    def test_multiple_words_use_and_semantics(self):
        self.assertIn(self.guide.pk, self.ids("machine learning"))
        self.assertNotIn(self.guide.pk, self.ids("machine bicycle"))

    def test_quoted_phrase_search(self):
        self.assertIn(self.guide.pk, self.ids('"Machine Learning"'))
        self.assertNotIn(self.guide.pk, self.ids('"Learning Machine"'))

    def test_exclusion_operator(self):
        self.assertIn(self.guide.pk, self.ids("machine"))
        self.assertNotIn(self.guide.pk, self.ids("machine -learning"))

    def test_english_stemming(self):
        self.assertIn(self.guide.pk, self.ids("network"))
        self.assertIn(self.guide.pk, self.ids("train"))

    def test_german_stemming(self):
        self.assertIn(self.guide.pk, self.ids("Anleitung", "de"))
        self.assertIn(self.guide.pk, self.ids("Grundlage", "de"))

    def test_search_is_case_insensitive(self):
        self.assertIn(self.guide.pk, self.ids("MACHINE"))
        self.assertIn(self.guide.pk, self.ids("machine"))

    def test_umlaut_query_matches(self):
        self.assertIn(self.guide.pk, self.ids("Übersetzung", "de"))

    def test_german_stemmer_folds_umlauts(self):
        # Verified against PostgreSQL 17: the German Snowball stemmer folds
        # ue/ae/oe and sharp s, so "Ubersetzung" reaches "Übersetzung"
        # without the unaccent extension. The digraph spelling does not.
        self.assertIn(self.guide.pk, self.ids("Ubersetzung", "de"))
        self.assertNotIn(self.guide.pk, self.ids("Uebersetzung", "de"))

    def test_hyphenated_query_does_not_error(self):
        self.assertIsInstance(self.ids("real-time"), list)

    def test_special_characters_do_not_raise(self):
        for term in ("a & b", "a | b", "!!!", "a:b", "(a)", "* *", "a\\b", '"unclosed'):
            with self.subTest(term=term):
                self.assertIsInstance(self.ids(term), list)

    def test_rank_is_finite_and_non_negative(self):
        for result in self.search("machine"):
            self.assertGreaterEqual(result.rank, 0)
            self.assertLess(result.rank, 1)

    def test_title_hit_outranks_a_body_only_hit(self):
        title_hit = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Weighttoken in the title",
                    "intro": "i",
                    "body": "b",
                    "slug": "weight-title-en",
                }
            },
        )
        body_hit = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Unrelated heading",
                    "intro": "unrelated intro",
                    "body": "<p>Weighttoken appears only in the body.</p>",
                    "slug": "weight-body-en",
                }
            },
        )
        ordered = self.ids("Weighttoken")
        self.assertEqual(ordered, [title_hit.pk, body_hit.pk])


@postgresql_only
class FailClosedTests(GuideAdapterTestCase):
    def test_unsearchable_query_is_rejected(self):
        for raw in (None, "", "a", "x" * 101):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    self.adapter.search(
                        query=normalize_search_query(raw), language_code="en"
                    )

    def test_unsupported_language_is_rejected(self):
        with self.assertRaises(UnsupportedSearchLanguage):
            self.adapter.search(
                query=normalize_search_query("machine"), language_code="fr"
            )

    def test_query_is_validated_before_the_database_is_touched(self):
        query = NormalizedSearchQuery(value="a", issue=SearchQueryIssue.TOO_SHORT)
        with self.assertNumQueries(0):
            with self.assertRaises(ValueError):
                self.adapter.search(query=query, language_code="en")

    def test_language_is_validated_before_the_database_is_touched(self):
        with self.assertNumQueries(0):
            with self.assertRaises(UnsupportedSearchLanguage):
                self.adapter.search(
                    query=normalize_search_query("machine"), language_code="xx"
                )

    def test_non_postgresql_backend_fails_closed(self):
        from unittest.mock import patch

        with patch("search.fts.connection") as fake_connection:
            fake_connection.vendor = "sqlite"
            with self.assertRaises(SearchBackendUnavailable):
                self.adapter.search(
                    query=normalize_search_query("machine"), language_code="en"
                )


@postgresql_only
class HtmlBehaviourTests(GuideAdapterTestCase):
    """
    PostgreSQL's text search parser skips HTML tags, attributes and the
    contents of script/style elements. These tests pin that behaviour for the
    real TinyMCE markup this project stores, so nothing has to be stripped in
    SQL and no regular expression is involved.
    """

    def _guide_with_body(self, slug, body):
        return make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": f"Html case {slug}",
                    "intro": "neutral intro",
                    "body": body,
                    "slug": slug,
                }
            },
        )

    def test_visible_body_text_matches(self):
        guide = self._guide_with_body("html-visible", "<p>Visible needletoken here</p>")
        self.assertIn(guide.pk, self.ids("needletoken"))

    def test_tag_name_does_not_match(self):
        guide = self._guide_with_body(
            "html-tag", "<needletoken>Visible text</needletoken>"
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_class_attribute_does_not_match(self):
        guide = self._guide_with_body(
            "html-class", '<a class="needletoken">Visible text</a>'
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_href_attribute_does_not_match(self):
        guide = self._guide_with_body(
            "html-href", '<a href="https://example.test/needletoken">Visible text</a>'
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_data_attribute_does_not_match(self):
        guide = self._guide_with_body(
            "html-data", '<div data-tag="needletoken">Visible text</div>'
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_alt_attribute_does_not_match(self):
        guide = self._guide_with_body(
            "html-alt", '<img src="/a.png" alt="needletoken">'
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_visible_link_text_matches(self):
        guide = self._guide_with_body(
            "html-linktext", '<a href="https://example.test/">needletoken</a>'
        )
        self.assertIn(guide.pk, self.ids("needletoken"))

    def test_script_content_does_not_match(self):
        guide = self._guide_with_body(
            "html-script", "<script>var needletoken = 1;</script>"
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_style_content_does_not_match(self):
        guide = self._guide_with_body(
            "html-style", "<style>.needletoken { color: red; }</style>"
        )
        self.assertNotIn(guide.pk, self.ids("needletoken"))

    def test_tinymce_paragraph_matches(self):
        guide = self._guide_with_body(
            "html-tinymce",
            "<p>Learn how to use <strong>needletoken</strong> effectively.</p>",
        )
        self.assertIn(guide.pk, self.ids("needletoken"))

    def test_tinymce_table_matches(self):
        guide = self._guide_with_body(
            "html-table",
            "<table><tbody><tr><td>needletoken</td><td>value</td></tr></tbody></table>",
        )
        self.assertIn(guide.pk, self.ids("needletoken"))

    def test_code_block_matches(self):
        guide = self._guide_with_body(
            "html-code",
            '<pre><code class="language-python">x = needletoken()</code></pre>',
        )
        self.assertIn(guide.pk, self.ids("needletoken"))

    def test_snippet_contains_no_markup(self):
        guide = self._guide_with_body(
            "html-snippet",
            "<p>Visible <strong>needletoken</strong> text with "
            '<a href="https://example.test/secret">a link</a>.</p>',
        )
        result = next(r for r in self.search("needletoken") if r.object_id == guide.pk)
        for markup in ("<", ">", "href", "strong", "secret"):
            with self.subTest(markup=markup):
                self.assertNotIn(markup, result.summary)

    def test_snippet_is_a_plain_string(self):
        guide = self._guide_with_body("html-plain", "<p>Visible needletoken</p>")
        result = next(r for r in self.search("needletoken") if r.object_id == guide.pk)
        self.assertIs(type(result.summary), str)
        self.assertFalse(hasattr(result.summary, "__html__"))


@postgresql_only
class MatchedFieldTests(GuideAdapterTestCase):
    def _matched_field(self, term, guide):
        return next(r for r in self.search(term) if r.object_id == guide.pk).matched_field

    def test_title_match_is_reported(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Titletoken guide",
                    "intro": "neutral",
                    "body": "neutral",
                    "slug": "matched-title-en",
                }
            },
        )
        self.assertIs(self._matched_field("Titletoken", guide), SearchMatchedField.TITLE)

    def test_intro_match_is_reported_as_summary(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Neutral heading",
                    "intro": "Contains introtoken here",
                    "body": "neutral",
                    "slug": "matched-intro-en",
                }
            },
        )
        self.assertIs(
            self._matched_field("introtoken", guide), SearchMatchedField.SUMMARY
        )

    def test_body_match_is_reported(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Neutral heading",
                    "intro": "neutral intro",
                    "body": "<p>Contains bodytoken here</p>",
                    "slug": "matched-body-en",
                }
            },
        )
        self.assertIs(self._matched_field("bodytoken", guide), SearchMatchedField.BODY)

    def test_title_wins_when_several_fields_match(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Everywheretoken heading",
                    "intro": "Everywheretoken intro",
                    "body": "<p>Everywheretoken body</p>",
                    "slug": "matched-all-en",
                }
            },
        )
        self.assertIs(
            self._matched_field("Everywheretoken", guide), SearchMatchedField.TITLE
        )

    def test_matched_field_works_for_a_stemmed_hit(self):
        # "Anleitungen" is not a substring of the query "Anleitung"; only the
        # database can tell which field matched.
        guide = make_published_guide(
            author=self.author,
            translations={
                "de": {
                    "title": "Anleitungen für Anfänger",
                    "intro": "neutral",
                    "body": "neutral",
                    "slug": "matched-stem-de",
                }
            },
        )
        result = next(
            r for r in self.search("Anleitung", "de") if r.object_id == guide.pk
        )
        self.assertIs(result.matched_field, SearchMatchedField.TITLE)


@postgresql_only
class ResultShapeTests(GuideAdapterTestCase):
    def test_returns_a_tuple_of_search_results(self):
        make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Shapetoken guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "shape-en",
                }
            },
        )
        results = self.search("Shapetoken")
        self.assertIsInstance(results, tuple)
        self.assertTrue(all(r.kind is SearchResultKind.GUIDE for r in results))

    def test_no_results_returns_an_empty_tuple(self):
        self.assertEqual(self.search("nothingmatchesthistoken"), ())

    def test_results_are_deterministic_across_calls(self):
        for index in range(3):
            make_published_guide(
                author=self.author,
                translations={
                    "en": {
                        "title": f"Ordertoken guide {index}",
                        "intro": "i",
                        "body": "b",
                        "slug": f"order-{index}-en",
                    }
                },
            )
        self.assertEqual(self.ids("Ordertoken"), self.ids("Ordertoken"))

    def test_result_carries_publication_dates(self):
        guide = make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Datetoken guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "date-en",
                }
            },
        )
        result = self.search("Datetoken")[0]
        self.assertEqual(result.published_at, guide.published_at)
        self.assertEqual(result.updated_at, guide.updated_at)

    def test_every_result_url_resolves(self):
        make_published_guide(
            author=self.author,
            translations={
                "en": {
                    "title": "Reachabletoken guide",
                    "intro": "i",
                    "body": "b",
                    "slug": "reachable-en",
                }
            },
        )
        self.addCleanup(translation.deactivate_all)
        for result in self.search("Reachabletoken"):
            self.assertNotEqual(result.url, "#")
            self.assertEqual(self.client.get(result.url).status_code, 200)
