"""
Beta 10.5: what is specific to the use case adapter.

Two decisions matter here: persona is not indexed, and the public queryset
is whatever the model decides.

Both moved in Beta 11.7. UseCaseQuerySet.visible_in_language() now uses
visible_on_site() like every other editorial model, so a use case mid-review
stays searchable under its published values instead of disappearing. And
persona is now snapshotted (it is rendered on the public card), so the
"nothing to search" argument for leaving it out no longer holds - but
indexing a new field changes ranking and snippets, which is a search
decision and stays out of that slice.
"""
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from core.projections import public_content_value
from search.adapters.usecases import USE_CASE_SEARCH_FIELDS, UseCaseSearchAdapter
from search.query import normalize_search_query
from search.result_types import SearchMatchedField, SearchResultKind
from search.tests.editorial_fixtures import (
    ADAPTER_SPECS,
    begin_unpublished_revision,
    edit_without_publishing,
    make_author,
    publish,
)
from usecases.models import UseCase

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)

USE_CASE_SPEC = next(spec for spec in ADAPTER_SPECS if spec.name == "usecase")


class UseCaseAdapterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author("usecase-adapter-editor")

    def setUp(self):
        self.adapter = UseCaseSearchAdapter()

    def search(self, term, language_code="en"):
        return self.adapter.search(
            query=normalize_search_query(term), language_code=language_code
        )

    def ids(self, term, language_code="en"):
        return [r.object_id for r in self.search(term, language_code)]

    def make(self, slug, **values):
        payload = {
            "title": "Neutral use case heading",
            "intro": "neutral intro",
            "body": "neutral body",
            "outro": "neutral outro",
            "slug": slug,
        }
        payload.update(values)
        return publish(USE_CASE_SPEC, author=self.author, translations={"en": payload})


class UseCaseFieldConfigurationTests(TestCase):
    def test_indexes_title_intro_body_and_outro(self):
        self.assertEqual(
            [f.public_field for f in USE_CASE_SEARCH_FIELDS],
            ["title", "intro", "body", "outro"],
        )

    def test_persona_is_not_indexed(self):
        self.assertNotIn(
            "persona", [f.public_field for f in USE_CASE_SEARCH_FIELDS]
        )

    def test_persona_is_snapshotted_but_still_deliberately_unindexed(self):
        # Beta 11.7 added persona to the snapshot so the public use-case card
        # can render it live-gated. Search deliberately did not follow.
        self.assertIn("persona", UseCase.LIVE_SNAPSHOT_FIELDS)
        self.assertNotIn("persona", [f.public_field for f in USE_CASE_SEARCH_FIELDS])

    def test_adapter_kind(self):
        self.assertIs(UseCaseSearchAdapter.kind, SearchResultKind.USE_CASE)


@postgresql_only
class UseCasePersonaTests(UseCaseAdapterTestCase):
    def test_public_projection_of_persona_is_empty_for_a_published_use_case(self):
        use_case = self.make("persona-projection-en")
        edit_without_publishing(use_case, language_code="en", persona="Freelancer")
        self.assertEqual(
            public_content_value(use_case, "persona", language_code="en"), ""
        )

    def test_persona_text_is_not_searchable(self):
        use_case = self.make("persona-search-en")
        edit_without_publishing(use_case, language_code="en", persona="Personaneedle")
        self.assertNotIn(use_case.pk, self.ids("Personaneedle"))

    def test_no_result_reports_a_metadata_match(self):
        use_case = self.make("persona-matched-en", intro="Contains introtoken")
        result = next(r for r in self.search("introtoken") if r.object_id == use_case.pk)
        self.assertIsNot(result.matched_field, SearchMatchedField.METADATA)


@postgresql_only
class UseCaseVisibilityTests(UseCaseAdapterTestCase):
    def test_review_with_live_revision_stays_searchable_under_published_values(self):
        # Beta 11.7: visible_in_language() uses visible_on_site(), so - like a
        # guide - the object keeps its public presence during review. Search
        # follows the model, and keeps matching the *published* text.
        use_case = self.make("uc-review-en", title="Reviewtoken use case")
        self.assertIn(use_case.pk, self.ids("Reviewtoken"))

        begin_unpublished_revision(
            use_case, author=self.author, language_code="en", intro="edited"
        )
        self.assertIn(use_case.pk, self.ids("Reviewtoken"))
        self.assertIn(
            use_case.pk,
            list(UseCase.objects.visible_in_language("en").values_list("pk", flat=True)),
        )

    def test_review_edit_text_is_not_searchable_before_republishing(self):
        use_case = self.make("uc-review-draft-en", title="Reviewdraft use case")
        begin_unpublished_revision(
            use_case, author=self.author, language_code="en", intro="Draftneedle intro"
        )
        self.assertNotIn(use_case.pk, self.ids("Draftneedle"))

    def test_search_matches_the_public_queryset(self):
        self.make("uc-parity-a-en", title="Paritytoken one")
        self.make("uc-parity-b-en", title="Paritytoken two")
        public_ids = set(
            UseCase.objects.visible_in_language("en").values_list("pk", flat=True)
        )
        self.assertTrue(set(self.ids("Paritytoken")).issubset(public_ids))


@postgresql_only
class UseCaseOutroTests(UseCaseAdapterTestCase):
    def test_published_outro_is_searchable(self):
        use_case = self.make("uc-outro-en", outro="Contains outrotoken here")
        self.assertIn(use_case.pk, self.ids("outrotoken"))

    def test_draft_outro_is_not_searchable(self):
        use_case = self.make("uc-outro-draft-en", outro="Published outrotoken")
        edit_without_publishing(use_case, language_code="en", outro="Draftneedle outro")
        self.assertIn(use_case.pk, self.ids("outrotoken"))
        self.assertNotIn(use_case.pk, self.ids("Draftneedle"))

    def test_outro_hit_reports_body(self):
        use_case = self.make("uc-outro-mf-en", outro="Contains outrotoken here")
        result = next(r for r in self.search("outrotoken") if r.object_id == use_case.pk)
        self.assertIs(result.matched_field, SearchMatchedField.BODY)
