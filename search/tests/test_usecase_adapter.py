"""
Beta 10.5: what is specific to the use case adapter.

Two decisions matter here: persona is not indexed because the model never
snapshots it, and the public queryset is the stricter published() rather than
visible_on_site().
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

    def test_persona_is_not_part_of_the_published_snapshot(self):
        # The reason persona cannot be indexed: the model never writes it into
        # live_i18n, so it has no published representation to search.
        self.assertNotIn("persona", UseCase.LIVE_SNAPSHOT_FIELDS)

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
    def test_review_with_live_revision_is_not_public(self):
        # UseCaseQuerySet.visible_in_language() uses published(), so unlike a
        # guide the object leaves public view during review. Search follows.
        use_case = self.make("uc-review-en", title="Reviewtoken use case")
        self.assertIn(use_case.pk, self.ids("Reviewtoken"))

        begin_unpublished_revision(
            use_case, author=self.author, language_code="en", intro="edited"
        )
        self.assertNotIn(use_case.pk, self.ids("Reviewtoken"))
        self.assertNotIn(
            use_case.pk,
            list(UseCase.objects.visible_in_language("en").values_list("pk", flat=True)),
        )

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
