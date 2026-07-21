"""
Beta 10.5: what is specific to the prompt adapter.

The shared editorial guarantees are covered in test_editorial_adapters.py;
this module pins the decisions only prompts carry - indexing the outro, and
searching the prompt text itself.
"""
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from search.adapters.prompts import PROMPT_SEARCH_FIELDS, PromptSearchAdapter
from search.query import normalize_search_query
from search.result_types import SearchMatchedField, SearchResultKind
from search.tests.editorial_fixtures import (
    ADAPTER_SPECS,
    edit_without_publishing,
    make_author,
    publish,
)

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)

PROMPT_SPEC = next(spec for spec in ADAPTER_SPECS if spec.name == "prompt")


class PromptAdapterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author("prompt-adapter-editor")

    def setUp(self):
        self.adapter = PromptSearchAdapter()

    def search(self, term, language_code="en"):
        return self.adapter.search(
            query=normalize_search_query(term), language_code=language_code
        )

    def ids(self, term, language_code="en"):
        return [r.object_id for r in self.search(term, language_code)]

    def make(self, slug, **values):
        payload = {
            "title": "Neutral prompt heading",
            "intro": "neutral intro",
            "body": "neutral body",
            "outro": "neutral outro",
            "slug": slug,
        }
        payload.update(values)
        return publish(PROMPT_SPEC, author=self.author, translations={"en": payload})


class PromptFieldConfigurationTests(TestCase):
    def test_indexes_title_intro_body_and_outro(self):
        self.assertEqual(
            [f.public_field for f in PROMPT_SEARCH_FIELDS],
            ["title", "intro", "body", "outro"],
        )

    def test_weights_follow_the_shared_scheme(self):
        weights = {f.public_field: f.weight for f in PROMPT_SEARCH_FIELDS}
        self.assertEqual(weights, {"title": "A", "intro": "B", "body": "C", "outro": "C"})

    def test_outro_reports_as_body(self):
        outro = next(f for f in PROMPT_SEARCH_FIELDS if f.public_field == "outro")
        self.assertIs(outro.matched_field, SearchMatchedField.BODY)

    def test_adapter_kind(self):
        self.assertIs(PromptSearchAdapter.kind, SearchResultKind.PROMPT)


@postgresql_only
class PromptOutroTests(PromptAdapterTestCase):
    def test_published_outro_is_searchable(self):
        prompt = self.make("outro-public-en", outro="Contains outrotoken here")
        self.assertIn(prompt.pk, self.ids("outrotoken"))

    def test_outro_hit_reports_body(self):
        prompt = self.make("outro-matched-en", outro="Contains outrotoken here")
        result = next(r for r in self.search("outrotoken") if r.object_id == prompt.pk)
        self.assertIs(result.matched_field, SearchMatchedField.BODY)

    def test_outro_hit_excerpts_the_outro(self):
        prompt = self.make(
            "outro-snippet-en",
            body="A body about something entirely different.",
            outro="Contains outrotoken and further closing guidance.",
        )
        result = next(r for r in self.search("outrotoken") if r.object_id == prompt.pk)
        self.assertIn("outrotoken", result.summary)
        self.assertNotIn("entirely different", result.summary)

    def test_draft_outro_is_not_searchable(self):
        prompt = self.make("outro-draft-en", outro="Published outrotoken")
        edit_without_publishing(prompt, language_code="en", outro="Draftneedle outro")
        self.assertIn(prompt.pk, self.ids("outrotoken"))
        self.assertNotIn(prompt.pk, self.ids("Draftneedle"))

    def test_draft_filling_an_empty_published_outro_is_not_searchable(self):
        prompt = self.make("outro-empty-en", outro="")
        edit_without_publishing(prompt, language_code="en", outro="Draftneedle outro")
        self.assertNotIn(prompt.pk, self.ids("Draftneedle"))

    def test_body_outranks_outro_when_both_match(self):
        # Both weigh C, so declaration order decides: body before outro.
        prompt = self.make(
            "outro-order-en",
            body="Sharedtoken in the body text.",
            outro="Sharedtoken in the outro text.",
        )
        result = next(r for r in self.search("Sharedtoken") if r.object_id == prompt.pk)
        self.assertIn("body text", result.summary)


@postgresql_only
class PromptBodyTests(PromptAdapterTestCase):
    def test_prompt_text_is_searchable(self):
        prompt = self.make(
            "prompt-text-en",
            body="<p>You are an expert copywriter. Write a promptneedle headline.</p>",
        )
        self.assertIn(prompt.pk, self.ids("promptneedle"))

    def test_long_prompt_body_is_searchable(self):
        prompt = self.make(
            "prompt-long-en",
            body="<p>" + ("Instruction sentence. " * 300) + "Finalneedle marker.</p>",
        )
        self.assertIn(prompt.pk, self.ids("Finalneedle"))

    def test_code_block_in_a_prompt_is_searchable(self):
        prompt = self.make(
            "prompt-code-en",
            body='<pre><code class="language-python">x = codeneedle()</code></pre>',
        )
        self.assertIn(prompt.pk, self.ids("codeneedle"))

    def test_attribute_in_a_prompt_body_does_not_match(self):
        prompt = self.make(
            "prompt-attr-en",
            body='<a href="https://example.test/attrneedle">Visible text</a>',
        )
        self.assertNotIn(prompt.pk, self.ids("attrneedle"))

    def test_body_snippet_is_plain_text(self):
        prompt = self.make(
            "prompt-plain-en",
            body="<p>Write a <strong>bodyneedle</strong> for the campaign.</p>",
        )
        result = next(r for r in self.search("bodyneedle") if r.object_id == prompt.pk)
        self.assertIn("bodyneedle", result.summary)
        self.assertNotIn("<", result.summary)
