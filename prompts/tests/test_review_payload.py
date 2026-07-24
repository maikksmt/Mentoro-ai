"""
Beta 11.11C1: the canonical Prompt review payload.

``build_prompt_review_payload()`` is a pure read-builder: given a saved
Prompt, it re-reads the database and returns a JSON-native dict describing
everything publicly relevant about the currently stored draft. These tests
hold it to that contract - source-of-truth correctness against a possibly
stale caller object, deterministic canonicalization independent of insert
order, a fixed and small query budget, complete mutation freedom, and a
sensitivity/exclusion matrix proving the fingerprint changes for exactly the
data points that matter and never for the ones that do not.
"""
import itertools
import json

from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from reversion.models import Revision, Version

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import fingerprint_review_payload
from guides.models import Guide
from prompts.models import Prompt, PromptTranslation
from prompts.review_payload import (
    CONTENT_TYPE,
    SCHEMA,
    TRANSLATION_FIELDS,
    PromptReviewPayloadError,
    PromptReviewPayloadErrorCode,
    build_prompt_review_payload,
)

User = get_user_model()

JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def refetch(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


def make_tool(slug, name):
    tool = Tool.objects.create(slug=slug)
    tool.create_translation("en", name=name)
    return tool


_slug_counter = itertools.count()


def make_full_prompt(
    *,
    author=None,
    translations=None,
    tools=(),
    tags=(),
    **extra,
):
    if translations is None:
        # A unique default slug per call - many tests call this repeatedly
        # without overriding `translations`, and PromptTranslation.slug is
        # globally unique.
        translations = (("en", "T EN", "i", "b", "o", f"s-en-{next(_slug_counter)}", None),)
    prompt = Prompt.objects.create(author=author, **extra)
    for language_code, title, intro, body, outro, slug, public_slug in translations:
        prompt.create_translation(
            language_code,
            title=title,
            intro=intro,
            body=body,
            outro=outro,
            slug=slug,
            public_slug=public_slug,
        )
    if tools:
        prompt.tools.add(*tools)
    if tags:
        prompt.tags.add(*tags)
    return prompt


def assert_json_native(test, value, *, path="$"):
    """Recursively asserts ``value`` contains only dict/list/str/int/float/
    bool/None - the exact allow-list ``fingerprint_review_payload()`` itself
    enforces, checked independently here so a violation is caught at the
    source rather than only when hashing is attempted."""
    if isinstance(value, dict):
        for key, item in value.items():
            test.assertIsInstance(key, str, f"{path}: dict key {key!r} is not str")
            assert_json_native(test, item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_json_native(test, item, path=f"{path}[{index}]")
        return
    test.assertIsInstance(
        value, JSON_SCALAR_TYPES, f"{path}: {type(value).__name__} is not JSON-native"
    )
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        import math

        test.assertFalse(math.isnan(value) or math.isinf(value), f"{path}: non-finite float")


def query_count_for(ctx, model):
    """Counts queries that reference ``model``'s table as an exact quoted SQL
    identifier - a bare substring match would also match e.g. the
    ``prompts_prompt_tools`` M2M through table when looking for
    ``prompts_prompt``."""
    quoted = f'"{model._meta.db_table}"'
    return sum(1 for q in ctx.captured_queries if quoted in q["sql"])


# ======================================================================
# Phase 3: API and error contract
# ======================================================================


class ErrorContractTests(TestCase):
    def test_error_codes_are_stable_distinct_strings(self):
        codes = {
            PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT,
            PromptReviewPayloadErrorCode.UNSAVED_OBJECT,
            PromptReviewPayloadErrorCode.INVALID_DATABASE_ALIAS,
            PromptReviewPayloadErrorCode.DATABASE_ALIAS_MISMATCH,
            PromptReviewPayloadErrorCode.OBJECT_NOT_FOUND,
        }
        self.assertEqual(len(codes), 5)
        for code in codes:
            self.assertIsInstance(code, str)

    def test_error_carries_its_code(self):
        try:
            build_prompt_review_payload(None)
        except PromptReviewPayloadError as exc:
            self.assertEqual(exc.code, PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT)
            self.assertIsInstance(exc, ValueError)
        else:
            self.fail("expected PromptReviewPayloadError")

    def test_none_is_rejected(self):
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(None)
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT)

    def test_translation_model_is_rejected(self):
        prompt = make_full_prompt()
        translation = prompt.translations.get(language_code="en")
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(translation)
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT)

    def test_queryset_is_rejected(self):
        make_full_prompt()
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(Prompt.objects.all())
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT)

    def test_list_is_rejected(self):
        prompt = make_full_prompt()
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload([prompt])
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT)

    def test_other_editorial_type_is_rejected(self):
        guide = Guide.objects.create()
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(guide)
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT)

    def test_unsaved_prompt_is_rejected(self):
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(Prompt())
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.UNSAVED_OBJECT)

    def test_deleted_row_raises_object_not_found(self):
        prompt = make_full_prompt()
        Prompt.objects.filter(pk=prompt.pk).delete()
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(prompt)
        self.assertEqual(ctx.exception.code, PromptReviewPayloadErrorCode.OBJECT_NOT_FOUND)

    def test_invalid_using_type_raises_type_error(self):
        prompt = make_full_prompt()
        with self.assertRaises(TypeError):
            build_prompt_review_payload(prompt, using=123)

    def test_unknown_alias_name_raises_invalid_database_alias(self):
        prompt = make_full_prompt()
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(prompt, using="not-a-real-alias")
        self.assertEqual(
            ctx.exception.code, PromptReviewPayloadErrorCode.INVALID_DATABASE_ALIAS
        )

    def test_unknown_alias_is_not_misclassified_as_a_mismatch(self):
        """An unknown alias must win over the mismatch check even though it
        also differs from the object's own alias - it is checked first."""
        prompt = make_full_prompt()
        self.assertEqual(prompt._state.db, DEFAULT_DB_ALIAS)
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(prompt, using="bogus")
        self.assertEqual(
            ctx.exception.code, PromptReviewPayloadErrorCode.INVALID_DATABASE_ALIAS
        )

    def test_explicit_alias_contradicting_the_objects_own_alias_is_a_mismatch(self):
        prompt = make_full_prompt()
        prompt._state.db = "not-default"
        with self.assertRaises(PromptReviewPayloadError) as ctx:
            build_prompt_review_payload(prompt, using=DEFAULT_DB_ALIAS)
        self.assertEqual(
            ctx.exception.code, PromptReviewPayloadErrorCode.DATABASE_ALIAS_MISMATCH
        )

    def test_explicit_alias_matching_the_objects_own_alias_is_accepted(self):
        prompt = make_full_prompt()
        payload = build_prompt_review_payload(prompt, using=DEFAULT_DB_ALIAS)
        self.assertEqual(payload["schema"], SCHEMA)

    def test_early_errors_perform_zero_queries(self):
        prompt = make_full_prompt()
        with self.assertNumQueries(0):
            with self.assertRaises(PromptReviewPayloadError):
                build_prompt_review_payload(None)
        with self.assertNumQueries(0):
            with self.assertRaises(PromptReviewPayloadError):
                build_prompt_review_payload(Prompt())
        with self.assertNumQueries(0):
            with self.assertRaises(PromptReviewPayloadError):
                build_prompt_review_payload(prompt, using="bogus-alias")


# ======================================================================
# Phase 4/8: payload shape, schema, top-level determinism
# ======================================================================


class PayloadShapeTests(TestCase):
    def test_schema_and_content_type_are_stable(self):
        prompt = make_full_prompt()
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["schema"], "prompt-review-v2")
        self.assertEqual(payload["content_type"], "prompt")
        self.assertEqual(payload["schema"], SCHEMA)
        self.assertEqual(payload["content_type"], CONTENT_TYPE)

    def test_top_level_keys_are_exactly_the_documented_set(self):
        prompt = make_full_prompt()
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(set(payload), {"schema", "content_type", "fields", "translations", "relations"})
        self.assertEqual(set(payload["relations"]), {"author", "tools", "tags"})

    def test_fields_is_empty_because_prompt_has_no_root_content_field(self):
        """Documents the finding rather than assuming it: Prompt carries no
        untranslated content field of its own beyond the shared, excluded
        editorial workflow fields."""
        prompt = make_full_prompt()
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["fields"], {})

    def test_every_status_can_be_read(self):
        for status in (
            Workflow.STATUS_DRAFT,
            Workflow.STATUS_REVIEW,
            Workflow.STATUS_APPROVED,
            Workflow.STATUS_PUBLISHED,
            Workflow.STATUS_ARCHIVED,
        ):
            with self.subTest(status=status):
                prompt = make_full_prompt(status=status)
                payload = build_prompt_review_payload(prompt)
                self.assertEqual(payload["schema"], SCHEMA)

    def test_the_public_visibility_manager_is_not_used(self):
        """A draft prompt (never visible on the public site) must still be
        readable - proves .published (PublishedOnlyManager) is not what the
        builder queries through."""
        prompt = make_full_prompt(status=Workflow.STATUS_DRAFT)
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(len(payload["translations"]), 1)

    def test_payload_contains_only_json_native_types(self):
        tool = make_tool("json-native-tool", "JSON Tool")
        author = User.objects.create_user("json-native-author", password="pw")
        prompt = make_full_prompt(
            author=author,
            translations=(
                ("en", "T", "i", "b", "o", "json-native-en", "json-native-public"),
                ("de", "T2", "i2", "b2", "o2", "json-native-de", None),
            ),
            tools=(tool,),
            tags=("alpha",),
        )
        payload = build_prompt_review_payload(prompt)
        assert_json_native(self, payload)

    def test_fingerprint_accepts_every_generated_payload(self):
        tool = make_tool("fp-accept-tool", "Tool")
        author = User.objects.create_user("fp-accept-author", password="pw")
        prompt = make_full_prompt(author=author, tools=(tool,), tags=("alpha", "beta"))
        payload = build_prompt_review_payload(prompt)
        digest = fingerprint_review_payload(payload)
        self.assertEqual(len(digest), 64)

    def test_payload_is_fully_json_serializable(self):
        prompt = make_full_prompt()
        payload = build_prompt_review_payload(prompt)
        round_tripped = json.loads(json.dumps(payload))
        self.assertEqual(round_tripped, payload)


# ======================================================================
# Phase 5: translations
# ======================================================================


class TranslationContractTests(TestCase):
    def test_english_only(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "o", "trans-en-only", None),))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual([t["language_code"] for t in payload["translations"]], ["en"])

    def test_german_only(self):
        prompt = make_full_prompt(translations=(("de", "T", "i", "b", "o", "trans-de-only", None),))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual([t["language_code"] for t in payload["translations"]], ["de"])

    def test_english_and_german_sorted_by_language_code(self):
        prompt = make_full_prompt(
            translations=(
                ("de", "T DE", "i", "b", "o", "trans-both-de", None),
                ("en", "T EN", "i", "b", "o", "trans-both-en", None),
            )
        )
        payload = build_prompt_review_payload(prompt)
        self.assertEqual([t["language_code"] for t in payload["translations"]], ["de", "en"])

    def test_a_configured_third_language_is_included(self):
        """PARLER_LANGUAGES only configures en/de, but PromptTranslation's
        language_code is a plain CharField with no FK/choices constraint at
        the database level, so a differently-coded row is still a real,
        storable translation the builder must not silently drop."""
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "o", "trans-extra-en", None),))
        PromptTranslation.objects.create(
            master=prompt, language_code="fr", title="T FR", intro="i", body="b",
            outro="o", slug="trans-extra-fr",
        )
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(
            [t["language_code"] for t in payload["translations"]], ["en", "fr"]
        )

    def test_a_translation_with_only_empty_fields_stays_in_the_payload(self):
        prompt = Prompt.objects.create()
        prompt.create_translation(
            "en", title="", intro="", body="", outro="", slug="trans-empty-en"
        )
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(len(payload["translations"]), 1)
        self.assertEqual(payload["translations"][0]["title"], "")

    def test_adding_a_language_changes_the_translation_list(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "o", "trans-add-en", None),))
        before = build_prompt_review_payload(prompt)
        prompt.create_translation("de", title="T2", intro="i2", body="b2", outro="o2", slug="trans-add-de")
        after = build_prompt_review_payload(prompt)
        self.assertNotEqual(before["translations"], after["translations"])
        self.assertEqual(len(after["translations"]), 2)

    def test_removing_a_language_changes_the_translation_list(self):
        prompt = make_full_prompt(
            translations=(
                ("en", "T", "i", "b", "o", "trans-remove-en", None),
                ("de", "T2", "i2", "b2", "o2", "trans-remove-de", None),
            )
        )
        before = build_prompt_review_payload(prompt)
        prompt.delete_translation("de")
        after = build_prompt_review_payload(prompt)
        self.assertNotEqual(before["translations"], after["translations"])
        self.assertEqual(len(after["translations"]), 1)

    def test_creation_order_does_not_affect_the_result(self):
        prompt_a = make_full_prompt(
            translations=(
                ("en", "T", "i", "b", "o", "trans-order-a-en", None),
                ("de", "T2", "i2", "b2", "o2", "trans-order-a-de", None),
            )
        )
        prompt_b = make_full_prompt(
            translations=(
                ("de", "T2", "i2", "b2", "o2", "trans-order-b-de", None),
                ("en", "T", "i", "b", "o", "trans-order-b-en", None),
            )
        )
        payload_a = build_prompt_review_payload(prompt_a)
        payload_b = build_prompt_review_payload(prompt_b)
        # Compare shape (language ordering, field set), not slugs, which
        # necessarily differ between two distinct prompts.
        self.assertEqual(
            [t["language_code"] for t in payload_a["translations"]],
            [t["language_code"] for t in payload_b["translations"]],
        )

    def test_active_caller_language_en_has_no_effect(self):
        from django.utils import translation as dj_translation

        prompt = make_full_prompt(
            translations=(
                ("en", "T EN", "i", "b", "o", "trans-lang-en", None),
                ("de", "T DE", "i2", "b2", "o2", "trans-lang-de", None),
            )
        )
        with dj_translation.override("en"):
            payload_en_active = build_prompt_review_payload(prompt)
        with dj_translation.override("de"):
            payload_de_active = build_prompt_review_payload(prompt)
        self.assertEqual(payload_en_active["translations"], payload_de_active["translations"])

    def test_no_parler_fallback_is_used(self):
        """Only a genuinely stored 'de' translation counts - no cross-language
        substitution the way safe_translation_getter() would perform."""
        prompt = make_full_prompt(translations=(("en", "T EN", "i", "b", "o", "trans-fallback-en", None),))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(len(payload["translations"]), 1)
        self.assertEqual(payload["translations"][0]["language_code"], "en")

    def test_all_six_whitelisted_translation_fields_are_present(self):
        prompt = make_full_prompt(
            translations=(("en", "T", "i", "b", "o", "trans-fields-en", "trans-fields-pub"),)
        )
        payload = build_prompt_review_payload(prompt)
        row = payload["translations"][0]
        self.assertEqual(
            set(row), {"language_code", *TRANSLATION_FIELDS}
        )
        self.assertEqual(TRANSLATION_FIELDS, ("title", "intro", "body", "outro", "slug", "public_slug"))


# ======================================================================
# Phase 6: author relation
# ======================================================================


class AuthorRelationTests(TestCase):
    """Beta 11.11C4D: the author relation is exactly ``{"id": author_id}`` -
    the redactional identity is the account, never the account's current
    display name (see the module docstring and the C4C audit). Name/username
    changes must leave the payload byte-for-byte identical; only a real
    reassignment of ``author_id`` may change it."""

    def test_no_author_serializes_as_none(self):
        prompt = make_full_prompt(author=None)
        payload = build_prompt_review_payload(prompt)
        self.assertIsNone(payload["relations"]["author"])

    def test_author_is_serialized_as_id_only(self):
        user = User.objects.create_user(
            "author-id-only", password="pw", first_name="Ada", last_name="Lovelace"
        )
        prompt = make_full_prompt(author=user)
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["author"], {"id": user.pk})

    def test_author_id_is_used_even_with_no_name_fields_set(self):
        user = User.objects.create_user("author-no-names", password="pw")
        prompt = make_full_prompt(author=user)
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["author"], {"id": user.pk})

    def test_author_email_is_never_included(self):
        user = User.objects.create_user(
            "author-email-check", password="pw", email="secret@example.com"
        )
        prompt = make_full_prompt(author=user)
        payload = build_prompt_review_payload(prompt)
        serialized = json.dumps(payload)
        self.assertNotIn("secret@example.com", serialized)
        self.assertNotIn("email", payload["relations"]["author"])

    def test_author_section_contains_only_id(self):
        user = User.objects.create_superuser(
            "author-super", "author-super@example.com", "pw"
        )
        prompt = make_full_prompt(author=user)
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(set(payload["relations"]["author"]), {"id"})

    def test_username_and_display_name_are_never_included(self):
        user = User.objects.create_user(
            "author-shape-check", password="pw", first_name="Ada", last_name="Lovelace"
        )
        prompt = make_full_prompt(author=user)
        payload = build_prompt_review_payload(prompt)
        self.assertNotIn("username", payload["relations"]["author"])
        self.assertNotIn("display_name", payload["relations"]["author"])

    def test_changing_the_author_assignment_changes_the_payload(self):
        author_a = User.objects.create_user("author-change-a", password="pw")
        author_b = User.objects.create_user("author-change-b", password="pw")
        prompt = make_full_prompt(author=author_a)
        before = build_prompt_review_payload(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["author"], after["relations"]["author"])

    def test_clearing_the_author_assignment_changes_the_payload(self):
        author = User.objects.create_user("author-clear", password="pw")
        prompt = make_full_prompt(author=author)
        before = build_prompt_review_payload(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(author=None)
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["author"], after["relations"]["author"])
        self.assertIsNone(after["relations"]["author"])

    def test_assigning_an_author_where_there_was_none_changes_the_payload(self):
        author = User.objects.create_user("author-assign", password="pw")
        prompt = make_full_prompt(author=None)
        before = build_prompt_review_payload(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(author=author)
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["author"], after["relations"]["author"])

    def test_changing_the_authors_first_name_does_not_change_the_payload(self):
        author = User.objects.create_user(
            "author-rename-first", password="pw", first_name="Old", last_name="Name"
        )
        prompt = make_full_prompt(author=author)
        before = build_prompt_review_payload(prompt)
        User.objects.filter(pk=author.pk).update(first_name="New")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertEqual(before["relations"]["author"], after["relations"]["author"])

    def test_changing_the_authors_last_name_does_not_change_the_payload(self):
        author = User.objects.create_user(
            "author-rename-last", password="pw", first_name="First", last_name="Old"
        )
        prompt = make_full_prompt(author=author)
        before = build_prompt_review_payload(prompt)
        User.objects.filter(pk=author.pk).update(last_name="New")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertEqual(before["relations"]["author"], after["relations"]["author"])

    def test_changing_both_names_at_once_does_not_change_the_payload(self):
        author = User.objects.create_user(
            "author-rename-both", password="pw", first_name="Old", last_name="Name"
        )
        prompt = make_full_prompt(author=author)
        before = build_prompt_review_payload(prompt)
        User.objects.filter(pk=author.pk).update(first_name="New", last_name="Person")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertEqual(before["relations"]["author"], after["relations"]["author"])

    def test_clearing_the_authors_name_to_blank_does_not_change_the_payload(self):
        author = User.objects.create_user(
            "author-rename-blank", password="pw", first_name="First", last_name="Last"
        )
        prompt = make_full_prompt(author=author)
        before = build_prompt_review_payload(prompt)
        User.objects.filter(pk=author.pk).update(first_name="", last_name="")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertEqual(before["relations"]["author"], after["relations"]["author"])

    def test_changing_the_username_does_not_change_the_payload(self):
        author = User.objects.create_user("author-rename-username-old", password="pw")
        prompt = make_full_prompt(author=author)
        before = build_prompt_review_payload(prompt)
        User.objects.filter(pk=author.pk).update(username="author-rename-username-new")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertEqual(before["relations"]["author"], after["relations"]["author"])

    def test_stale_caller_author_id_is_ignored_in_favor_of_the_database(self):
        author_a = User.objects.create_user("author-stale-a", password="pw")
        author_b = User.objects.create_user("author-stale-b", password="pw")
        prompt = make_full_prompt(author=author_a)
        stale = refetch(prompt)
        _ = stale.author  # populate the caller's own cached FK access
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        payload = build_prompt_review_payload(stale)
        self.assertEqual(payload["relations"]["author"], {"id": author_b.pk})

    def test_no_query_against_the_user_table(self):
        author = User.objects.create_user("author-lazy", password="pw")
        prompt = make_full_prompt(author=author)
        with CaptureQueriesContext(connection) as ctx:
            build_prompt_review_payload(prompt)
        self.assertEqual(query_count_for(ctx, User), 0)


# ======================================================================
# Phase 7: tags
# ======================================================================


class TagRelationTests(TestCase):
    def test_no_tags(self):
        prompt = make_full_prompt(tags=())
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["tags"], [])

    def test_one_tag(self):
        prompt = make_full_prompt(tags=("solo",))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["tags"], [{"slug": "solo", "name": "solo"}])

    def test_many_tags_are_sorted_by_slug(self):
        prompt = make_full_prompt(tags=("zeta", "alpha", "mu"))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(
            [t["slug"] for t in payload["relations"]["tags"]], ["alpha", "mu", "zeta"]
        )

    def test_same_tag_set_in_a_different_membership_creation_order_is_identical(self):
        prompt_a = make_full_prompt(tags=("alpha", "beta", "gamma"))
        prompt_b = make_full_prompt(tags=("gamma", "alpha", "beta"))
        payload_a = build_prompt_review_payload(prompt_a)
        payload_b = build_prompt_review_payload(prompt_b)
        self.assertEqual(payload_a["relations"]["tags"], payload_b["relations"]["tags"])

    def test_adding_a_tag_changes_the_payload(self):
        prompt = make_full_prompt(tags=("alpha",))
        before = build_prompt_review_payload(prompt)
        prompt.tags.add("beta")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["tags"], after["relations"]["tags"])

    def test_removing_a_tag_changes_the_payload(self):
        prompt = make_full_prompt(tags=("alpha", "beta"))
        before = build_prompt_review_payload(prompt)
        prompt.tags.remove("beta")
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["tags"], after["relations"]["tags"])

    def test_the_taggit_manager_is_never_mutated(self):
        prompt = make_full_prompt(tags=("alpha", "beta"))
        before = sorted(prompt.tags.names())
        build_prompt_review_payload(prompt)
        after = sorted(refetch(prompt).tags.names())
        self.assertEqual(before, after)

    def test_no_n_plus_one_for_many_tags(self):
        prompt = make_full_prompt(tags=tuple(f"tag-{i}" for i in range(25)))
        with CaptureQueriesContext(connection) as ctx:
            payload = build_prompt_review_payload(prompt)
        self.assertEqual(len(payload["relations"]["tags"]), 25)
        # exactly one query touches the tag table regardless of tag count
        from taggit.models import Tag as TaggitTag

        self.assertEqual(query_count_for(ctx, TaggitTag), 1)


# ======================================================================
# Phase 6/7 continued: tool relation
# ======================================================================


class ToolRelationTests(TestCase):
    def test_no_tools(self):
        prompt = make_full_prompt(tools=())
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["tools"], [])

    def test_tool_membership_only_no_display_attributes(self):
        tool = make_tool("tool-membership", "Tool Membership Name")
        prompt = make_full_prompt(tools=(tool,))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["tools"], [tool.pk])

    def test_many_tools_sorted_ascending(self):
        tool_a = make_tool("tool-sort-a", "A")
        tool_b = make_tool("tool-sort-b", "B")
        prompt = make_full_prompt(tools=(tool_b, tool_a))
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["relations"]["tools"], sorted([tool_a.pk, tool_b.pk]))

    def test_membership_creation_order_does_not_affect_the_result(self):
        tool_a = make_tool("tool-order-a", "A")
        tool_b = make_tool("tool-order-b", "B")
        prompt_a = make_full_prompt(tools=(tool_a, tool_b))
        prompt_b = make_full_prompt(tools=(tool_b, tool_a))
        payload_a = build_prompt_review_payload(prompt_a)
        payload_b = build_prompt_review_payload(prompt_b)
        self.assertEqual(payload_a["relations"]["tools"], sorted([tool_a.pk, tool_b.pk]))
        self.assertEqual(payload_b["relations"]["tools"], sorted([tool_a.pk, tool_b.pk]))

    def test_adding_a_tool_changes_the_payload(self):
        tool = make_tool("tool-add", "Add Tool")
        prompt = make_full_prompt(tools=())
        before = build_prompt_review_payload(prompt)
        prompt.tools.add(tool)
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["tools"], after["relations"]["tools"])

    def test_removing_a_tool_changes_the_payload(self):
        tool = make_tool("tool-remove", "Remove Tool")
        prompt = make_full_prompt(tools=(tool,))
        before = build_prompt_review_payload(prompt)
        prompt.tools.remove(tool)
        after = build_prompt_review_payload(refetch(prompt))
        self.assertNotEqual(before["relations"]["tools"], after["relations"]["tools"])

    def test_changing_a_linked_tools_own_name_does_not_change_the_payload(self):
        """Deliberate: Tool has no editorial workflow of its own, so its
        display attributes are excluded (see the module docstring)."""
        tool = make_tool("tool-rename-noeffect", "Original Name")
        prompt = make_full_prompt(tools=(tool,))
        before = build_prompt_review_payload(prompt)
        tool.set_current_language("en")
        tool.name = "Renamed"
        tool.save()
        after = build_prompt_review_payload(refetch(prompt))
        self.assertEqual(before["relations"]["tools"], after["relations"]["tools"])
        self.assertEqual(before, after)


# ======================================================================
# Phase 9: sensitivity matrix
# ======================================================================


class SensitivityMatrixTests(TestCase):
    """Change exactly one stored data point; the fingerprint must change."""

    def _fp(self, prompt):
        return fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))

    def test_title_change(self):
        prompt = make_full_prompt(translations=(("en", "Before", "i", "b", "o", "sens-title", None),))
        before = self._fp(prompt)
        prompt.set_current_language("en")
        prompt.title = "After"
        prompt.save()
        self.assertNotEqual(before, self._fp(prompt))

    def test_intro_change(self):
        prompt = make_full_prompt(translations=(("en", "T", "Before", "b", "o", "sens-intro", None),))
        before = self._fp(prompt)
        prompt.set_current_language("en")
        prompt.intro = "After"
        prompt.save()
        self.assertNotEqual(before, self._fp(prompt))

    def test_body_change(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "Before", "o", "sens-body", None),))
        before = self._fp(prompt)
        prompt.set_current_language("en")
        prompt.body = "After"
        prompt.save()
        self.assertNotEqual(before, self._fp(prompt))

    def test_outro_change(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "Before", "sens-outro", None),))
        before = self._fp(prompt)
        prompt.set_current_language("en")
        prompt.outro = "After"
        prompt.save()
        self.assertNotEqual(before, self._fp(prompt))

    def test_slug_change(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "o", "sens-slug-before", None),))
        before = self._fp(prompt)
        prompt.set_current_language("en")
        prompt.slug = "sens-slug-after"
        prompt.save()
        self.assertNotEqual(before, self._fp(prompt))

    def test_public_slug_change(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "o", "sens-pubslug", None),))
        before = self._fp(prompt)
        prompt.set_current_language("en")
        prompt.public_slug = "sens-pubslug-public"
        prompt.save()
        self.assertNotEqual(before, self._fp(prompt))

    def test_language_added(self):
        prompt = make_full_prompt(translations=(("en", "T", "i", "b", "o", "sens-lang-add", None),))
        before = self._fp(prompt)
        prompt.create_translation("de", title="T2", intro="i2", body="b2", outro="o2", slug="sens-lang-add-de")
        self.assertNotEqual(before, self._fp(prompt))

    def test_language_removed(self):
        prompt = make_full_prompt(
            translations=(
                ("en", "T", "i", "b", "o", "sens-lang-rm-en", None),
                ("de", "T2", "i2", "b2", "o2", "sens-lang-rm-de", None),
            )
        )
        before = self._fp(prompt)
        prompt.delete_translation("de")
        self.assertNotEqual(before, self._fp(prompt))

    def test_tag_added(self):
        prompt = make_full_prompt(tags=("alpha",))
        before = self._fp(prompt)
        prompt.tags.add("beta")
        self.assertNotEqual(before, self._fp(prompt))

    def test_tag_removed(self):
        prompt = make_full_prompt(tags=("alpha", "beta"))
        before = self._fp(prompt)
        prompt.tags.remove("beta")
        self.assertNotEqual(before, self._fp(prompt))

    def test_tag_slug_changed(self):
        prompt = make_full_prompt(tags=("original-slug",))
        before = self._fp(prompt)
        from taggit.models import Tag as TaggitTag

        TaggitTag.objects.filter(name="original-slug").update(slug="changed-slug")
        self.assertNotEqual(before, self._fp(prompt))

    def test_tag_name_changed(self):
        prompt = make_full_prompt(tags=("keepname",))
        before = self._fp(prompt)
        from taggit.models import Tag as TaggitTag

        TaggitTag.objects.filter(slug="keepname").update(name="renamed")
        self.assertNotEqual(before, self._fp(prompt))

    def test_tool_added(self):
        tool = make_tool("sens-tool-add", "Tool")
        prompt = make_full_prompt(tools=())
        before = self._fp(prompt)
        prompt.tools.add(tool)
        self.assertNotEqual(before, self._fp(prompt))

    def test_tool_removed(self):
        tool = make_tool("sens-tool-rm", "Tool")
        prompt = make_full_prompt(tools=(tool,))
        before = self._fp(prompt)
        prompt.tools.remove(tool)
        self.assertNotEqual(before, self._fp(prompt))

    def test_author_assignment_changed(self):
        author_a = User.objects.create_user("sens-author-a", password="pw")
        author_b = User.objects.create_user("sens-author-b", password="pw")
        prompt = make_full_prompt(author=author_a)
        before = self._fp(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        self.assertNotEqual(before, self._fp(prompt))

    def test_author_display_name_changed(self):
        """Beta 11.11C4D inverted this from the v1 contract: the author's
        first/last name is no longer part of the payload at all, so a pure
        display-name change must leave the fingerprint unchanged."""
        author = User.objects.create_user(
            "sens-author-display", password="pw", first_name="Old"
        )
        prompt = make_full_prompt(author=author)
        before = self._fp(prompt)
        User.objects.filter(pk=author.pk).update(first_name="New")
        self.assertEqual(before, self._fp(prompt))


# ======================================================================
# Phase 10: exclusion matrix
# ======================================================================


class ExclusionMatrixTests(TestCase):
    def _fp_and_payload(self, prompt):
        payload = build_prompt_review_payload(refetch(prompt))
        return payload, fingerprint_review_payload(payload)

    def _assert_unaffected(self, prompt, before_payload, before_fp):
        after_payload, after_fp = self._fp_and_payload(prompt)
        self.assertEqual(before_payload, after_payload)
        self.assertEqual(before_fp, after_fp)

    def setUp(self):
        self.reviewer = User.objects.create_user("exclusion-reviewer", password="pw")
        self.prompt = make_full_prompt(
            translations=(("en", "T", "i", "b", "o", "exclusion-en", None),)
        )
        self.before_payload, self.before_fp = self._fp_and_payload(self.prompt)

    def test_status_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(status=Workflow.STATUS_REVIEW)
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_review_revision_excluded(self):
        revision = Revision.objects.create(date_created=timezone.now(), comment="x")
        Prompt.objects.filter(pk=self.prompt.pk).update(review_revision=revision)
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_approved_revision_excluded(self):
        revision = Revision.objects.create(date_created=timezone.now(), comment="x")
        Prompt.objects.filter(pk=self.prompt.pk).update(approved_revision=revision)
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_review_payload_fingerprint_field_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(
            review_payload_fingerprint="a" * 64
        )
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_reviewed_by_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(reviewed_by=self.reviewer)
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_reviewed_at_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(reviewed_at=timezone.now())
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_submitted_for_review_at_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(submitted_for_review_at=timezone.now())
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_review_note_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(review_note="reviewer feedback")
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_created_at_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=30)
        )
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_updated_at_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(updated_at=timezone.now())
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_published_at_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(published_at=timezone.now())
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_is_published_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(is_published=True)
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_live_i18n_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(
            live_i18n={"en": {"title": "Published snapshot title"}}
        )
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_last_published_revision_id_excluded(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(last_published_revision_id=999)
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)

    def test_every_workflow_field_at_once_still_leaves_the_payload_unaffected(self):
        revision = Revision.objects.create(date_created=timezone.now(), comment="all-at-once")
        Prompt.objects.filter(pk=self.prompt.pk).update(
            status=Workflow.STATUS_APPROVED,
            review_revision=revision,
            approved_revision=revision,
            review_payload_fingerprint="b" * 64,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
            submitted_for_review_at=timezone.now(),
            review_note="combined feedback",
            updated_at=timezone.now(),
            published_at=timezone.now(),
            is_published=True,
            live_i18n={"en": {"title": "snapshot"}},
            last_published_revision_id=42,
        )
        self._assert_unaffected(self.prompt, self.before_payload, self.before_fp)


# ======================================================================
# Phase 11: stale caller contract
# ======================================================================


class StaleCallerTests(TestCase):
    def test_unsaved_local_field_change_is_ignored(self):
        prompt = make_full_prompt(translations=(("en", "DB Title", "i", "b", "o", "stale-field-en", None),))
        prompt.set_current_language("en")
        prompt.title = "Unsaved Local Title"
        # not saved
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["translations"][0]["title"], "DB Title")

    def test_database_change_after_load_is_reflected(self):
        prompt = make_full_prompt(translations=(("en", "Old Title", "i", "b", "o", "stale-db-en", None),))
        stale = refetch(prompt)
        fresh = Prompt.objects.get(pk=prompt.pk)
        fresh.set_current_language("en")
        fresh.title = "New Title"
        fresh.save()
        # `stale` is not refreshed.
        payload = build_prompt_review_payload(stale)
        self.assertEqual(payload["translations"][0]["title"], "New Title")

    def test_caller_translation_cache_does_not_override_the_database(self):
        prompt = make_full_prompt(translations=(("en", "Cached Title", "i", "b", "o", "stale-cache-en", None),))
        prompt.set_current_language("en")
        _ = prompt.title  # populate Parler's per-instance translation cache
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            title="Updated Directly"
        )
        payload = build_prompt_review_payload(prompt)
        self.assertEqual(payload["translations"][0]["title"], "Updated Directly")

    def test_prefetched_stale_tags_do_not_override_the_database(self):
        prompt = make_full_prompt(tags=("old-tag",))
        stale = refetch(prompt)
        list(stale.tags.all())  # force evaluation/caching on the stale instance
        Prompt.objects.get(pk=prompt.pk).tags.set(["new-tag"])
        payload = build_prompt_review_payload(stale)
        self.assertEqual(payload["relations"]["tags"], [{"slug": "new-tag", "name": "new-tag"}])

    def test_prefetched_stale_tools_do_not_override_the_database(self):
        tool_old = make_tool("stale-tool-old", "Old")
        tool_new = make_tool("stale-tool-new", "New")
        prompt = make_full_prompt(tools=(tool_old,))
        stale = refetch(prompt)
        list(stale.tools.all())
        Prompt.objects.get(pk=prompt.pk).tools.set([tool_new])
        payload = build_prompt_review_payload(stale)
        self.assertEqual(payload["relations"]["tools"], [tool_new.pk])


# ======================================================================
# Phase 12: query budget
# ======================================================================


class QueryBudgetTests(TestCase):
    EXPECTED_QUERIES = 4

    def test_empty_state_query_count(self):
        prompt = make_full_prompt(translations=(), tools=(), tags=())
        with CaptureQueriesContext(connection) as ctx:
            build_prompt_review_payload(prompt)
        self.assertEqual(len(ctx.captured_queries), self.EXPECTED_QUERIES)

    def test_small_state_query_count(self):
        tool = make_tool("budget-small-tool", "Tool")
        prompt = make_full_prompt(
            translations=(("en", "T", "i", "b", "o", "budget-small-en", None),),
            tools=(tool,),
            tags=("alpha",),
        )
        with CaptureQueriesContext(connection) as ctx:
            build_prompt_review_payload(prompt)
        self.assertEqual(len(ctx.captured_queries), self.EXPECTED_QUERIES)

    def test_large_state_query_count(self):
        tools = tuple(make_tool(f"budget-large-tool-{i}", f"Tool {i}") for i in range(10))
        translations = (
            ("en", "T", "i", "b", "o", "budget-large-en", None),
            ("de", "T2", "i2", "b2", "o2", "budget-large-de", None),
        )
        prompt = make_full_prompt(
            translations=translations,
            tools=tools,
            tags=tuple(f"budget-large-tag-{i}" for i in range(30)),
        )
        with CaptureQueriesContext(connection) as ctx:
            build_prompt_review_payload(prompt)
        self.assertEqual(len(ctx.captured_queries), self.EXPECTED_QUERIES)

    def test_query_breakdown_is_exactly_root_translations_tags_tools(self):
        from taggit.models import Tag as TaggitTag

        tool = make_tool("budget-breakdown-tool", "Tool")
        prompt = make_full_prompt(
            translations=(("en", "T", "i", "b", "o", "budget-breakdown-en", None),),
            tools=(tool,),
            tags=("alpha",),
        )
        with CaptureQueriesContext(connection) as ctx:
            build_prompt_review_payload(prompt)
        self.assertEqual(query_count_for(ctx, Prompt), 1)
        self.assertEqual(query_count_for(ctx, PromptTranslation), 1)
        self.assertEqual(query_count_for(ctx, TaggitTag), 1)
        self.assertEqual(query_count_for(ctx, Tool), 1)

    def test_no_reversion_table_query(self):
        prompt = make_full_prompt()
        with CaptureQueriesContext(connection) as ctx:
            build_prompt_review_payload(prompt)
        for table in (Revision._meta.db_table, Version._meta.db_table):
            with self.subTest(table=table):
                self.assertFalse(any(table in q["sql"] for q in ctx.captured_queries))

    def test_no_lazy_query_after_the_payload_is_returned(self):
        prompt = make_full_prompt(tags=("alpha",), translations=(("en", "T", "i", "b", "o", "budget-lazy-en", None),))
        with CaptureQueriesContext(connection) as ctx:
            payload = build_prompt_review_payload(prompt)
            queries_at_return = len(ctx.captured_queries)
            # touch every value in the returned structure
            json.dumps(payload)
        self.assertEqual(len(ctx.captured_queries), queries_at_return)


# ======================================================================
# Phase 13: mutation freedom
# ======================================================================


class MutationFreedomTests(TestCase):
    def test_a_full_prompt_state_is_left_completely_unchanged(self):
        reviewer = User.objects.create_user("mutation-reviewer", password="pw")
        author = User.objects.create_user("mutation-author", password="pw")
        tool = make_tool("mutation-tool", "Tool")
        revision = Revision.objects.create(date_created=timezone.now(), comment="mutation-freedom")

        prompt = make_full_prompt(
            author=author,
            translations=(
                ("en", "T EN", "i", "b", "o", "mutation-en", "mutation-pub-en"),
                ("de", "T DE", "i2", "b2", "o2", "mutation-de", None),
            ),
            tools=(tool,),
            tags=("alpha", "beta"),
            status=Workflow.STATUS_REVIEW,
            review_revision=revision,
            approved_revision=revision,
            review_payload_fingerprint="c" * 64,
            reviewed_by=reviewer,
            reviewed_at=timezone.now(),
            submitted_for_review_at=timezone.now(),
            review_note="keep me exactly as I am",
            live_i18n={"en": {"title": "live"}},
            last_published_revision_id=7,
        )

        def snapshot():
            fresh = Prompt.objects.get(pk=prompt.pk)
            return {
                "status": fresh.status,
                "review_revision_id": fresh.review_revision_id,
                "approved_revision_id": fresh.approved_revision_id,
                "review_payload_fingerprint": fresh.review_payload_fingerprint,
                "reviewed_by_id": fresh.reviewed_by_id,
                "reviewed_at": fresh.reviewed_at,
                "submitted_for_review_at": fresh.submitted_for_review_at,
                "review_note": fresh.review_note,
                "live_i18n": fresh.live_i18n,
                "last_published_revision_id": fresh.last_published_revision_id,
                "updated_at": fresh.updated_at,
                "translations": sorted(
                    fresh.translations.values_list("language_code", "title", "slug")
                ),
                "tags": sorted(fresh.tags.names()),
                "tools": sorted(fresh.tools.values_list("pk", flat=True)),
            }

        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()
        before = snapshot()

        with CaptureQueriesContext(connection) as ctx:
            for _ in range(3):
                build_prompt_review_payload(Prompt.objects.get(pk=prompt.pk))

        after = snapshot()

        self.assertEqual(before, after)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

        for query in ctx.captured_queries:
            verb = query["sql"].strip().split(None, 1)[0].upper()
            with self.subTest(sql=query["sql"][:80]):
                self.assertIn(verb, ("SELECT",))

    def test_active_parler_language_of_the_caller_object_is_unchanged(self):
        prompt = make_full_prompt(
            translations=(
                ("en", "T EN", "i", "b", "o", "mutation-lang-en", None),
                ("de", "T DE", "i2", "b2", "o2", "mutation-lang-de", None),
            )
        )
        prompt.set_current_language("de")
        build_prompt_review_payload(prompt)
        self.assertEqual(prompt.get_current_language(), "de")


# ======================================================================
# Phase 14: no runtime activation
# ======================================================================


class NoRuntimeActivationTests(TestCase):
    def test_no_unsanctioned_production_module_imports_or_calls_the_builder(self):
        import ast
        import pathlib

        import prompts.review_approval as review_approval_module
        import prompts.review_edit_guard as review_edit_guard_module
        import prompts.review_payload as review_payload_module
        import prompts.review_submission as review_submission_module

        symbols = (
            "build_prompt_review_payload",
            "PromptReviewPayloadError",
            "PromptReviewPayloadErrorCode",
        )
        # Beta 11.11C2A made prompts/review_submission.py the first sanctioned
        # consumer of the C1 builder: submit_prompt_for_review() feeds the
        # canonical payload into fingerprint_review_payload(). Beta 11.11C3A
        # added prompts/review_approval.py as the second: approve_prompt_review()
        # re-checks the same payload before and after approval. Beta 11.11C4G
        # added prompts/review_edit_guard.py as the third: its two-phase
        # capture/compare API rebuilds the same canonical payload before and
        # after a caller's own content mutation - itself not yet activated by
        # any production consumer (see
        # prompts/tests/test_review_edit_guard.py::NoRuntimeActivationTests).
        # All three are allowed importers alongside the builder's own module.
        # The builder is still not wired into any admin/model/view/signal/
        # search path - that is covered by C2A's/C3A's/C4G's own
        # no-runtime-activation tests and by
        # test_admin_module_never_imports_the_builder below.
        allowed_files = {
            pathlib.Path(review_payload_module.__file__).resolve(),
            pathlib.Path(review_submission_module.__file__).resolve(),
            pathlib.Path(review_approval_module.__file__).resolve(),
            pathlib.Path(review_edit_guard_module.__file__).resolve(),
        }

        project_root = pathlib.Path(review_payload_module.__file__).resolve().parents[1]
        offenders = []
        for py_file in project_root.rglob("*.py"):
            if "venv" in py_file.parts or "migrations" in py_file.parts:
                continue
            if "/tests/" in str(py_file) or py_file.name.startswith("test_"):
                continue
            resolved = py_file.resolve()
            if resolved in allowed_files:
                continue
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if not any(symbol in text for symbol in symbols):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = getattr(node, "module", None) or ""
                    names = [a.name for a in node.names]
                    if "review_payload" in module_name or any(s in names for s in symbols):
                        offenders.append(f"import of {symbols} in {py_file}")
                if isinstance(node, ast.Name) and node.id in symbols:
                    offenders.append(f"reference to {node.id} in {py_file}")

        self.assertEqual(offenders, [])

    def test_admin_module_never_imports_the_builder(self):
        import prompts.admin

        source = open(prompts.admin.__file__, encoding="utf-8").read()
        for symbol in (
            "build_prompt_review_payload",
            "review_payload",
            "PromptReviewPayloadError",
        ):
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, source)

    def test_submit_still_computes_no_payload_or_fingerprint(self):
        author = User.objects.create_user("no-activation-author", password="pw")
        prompt = make_full_prompt(author=author, status=Workflow.STATUS_DRAFT)
        prompt.move_to_review(by=author)
        prompt.save(update_fields=["status", "submitted_for_review_at", "updated_at"])
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertIsNone(reloaded.review_revision_id)
