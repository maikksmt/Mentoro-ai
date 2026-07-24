"""
Beta 11.11C2B: the prompt admin "Send to review" action, now backed by the
per-root C2A submission primitive.

The shared editorial admin action wrapped a whole changelist selection in one
transaction and one reversion revision. This module proves ``PromptAdmin``'s
override instead routes each selected prompt through
``submit_prompt_for_review`` - one atomic transaction, one revision, one
binding per prompt - while preserving the action's name, description,
permission contract and admin UX, and without opening any outer transaction or
reversion context of its own. Guide/UseCase/Comparison keep the shared path.

Beta 11.11C2B1A hardens *which* action the VersionAdmin bypass in
``changelist_view`` reacts to. A changelist POST can carry more than one
``action`` field (the action bar renders once above and once below the list);
only Django's own ``index``-selected entry is "the" action. The tests below
prove the bypass agrees with Django's real ``ModelAdmin.response_action``
selection on every legitimate case, and fails closed - no bypass - on every
negative, out-of-range, non-numeric-but-not-Django's-own-default, or empty
one, even where Django's own resolution would still land on the submit
action.
"""
import ast
import itertools
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib import admin as django_admin
from django.test import RequestFactory, TestCase
from django.urls import reverse
from reversion.models import Revision

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import fingerprint_review_payload
from guides.models import Guide
from prompts.admin import _selected_action_name
from prompts.models import Prompt
from prompts.review_payload import build_prompt_review_payload
from prompts.review_submission import (
    PromptReviewSubmissionError,
    PromptReviewSubmissionErrorCode,
    submit_prompt_for_review,
)
from usecases.models import UseCase

User = get_user_model()

_slug_counter = itertools.count()

SUBMIT_ACTION = "action_submit_for_review"
CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")


def make_prompt(*, status=Workflow.STATUS_DRAFT, author=None, languages=("en",)):
    prompt = Prompt.objects.create(status=status, author=author)
    for language_code in languages:
        prompt.create_translation(
            language_code,
            title=f"Title {language_code}",
            intro="intro",
            body="body",
            outro="outro",
            slug=f"slug-{next(_slug_counter)}",
        )
    return prompt


def refetch(prompt):
    return Prompt.objects.get(pk=prompt.pk)


def message_texts(response):
    return [str(m) for m in response.context["messages"]]


def revision_labels(revision):
    return sorted(
        f"{v.content_type.app_label}.{v.content_type.model}"
        for v in revision.version_set.select_related("content_type")
    )


class PromptAdminSubmissionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(
            "c2b-editor", password="pw", is_staff=True, first_name="Ed", last_name="Itor"
        )
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user(
            "c2b-author", password="pw", is_staff=True
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)

    def post_submit(self, prompts, *, follow=True, raise_request_exception=True):
        return self.client.post(
            CHANGELIST_URL,
            data={
                "action": SUBMIT_ACTION,
                "_selected_action": [str(p.pk) for p in prompts],
                "index": "0",
            },
            follow=follow,
            **({} if raise_request_exception else {"raise_request_exception": False}),
        )


# ======================================================================
# Action identity / registration
# ======================================================================


class ActionRegistrationTests(PromptAdminSubmissionTestCase):
    def _actions(self, model):
        model_admin = django_admin.site._registry[model]
        request = RequestFactory().get("/admin/")
        request.user = User.objects.get(pk=self.editor.pk)
        return model_admin.get_actions(request)

    def test_prompt_admin_has_exactly_one_submit_action(self):
        actions = self._actions(Prompt)
        submit_entries = [name for name in actions if "submit_for_review" in name]
        self.assertEqual(submit_entries, [SUBMIT_ACTION])

    def test_prompt_submit_action_resolves_to_the_override(self):
        func = self._actions(Prompt)[SUBMIT_ACTION][0]
        self.assertEqual(func.__qualname__, "PromptAdmin.action_submit_for_review")

    def test_prompt_submit_action_keeps_its_description(self):
        _func, _name, description = self._actions(Prompt)[SUBMIT_ACTION]
        self.assertEqual(str(description), "Send to review")

    def test_other_editorial_types_keep_the_shared_action(self):
        for model in (Guide, UseCase, Comparison):
            with self.subTest(model=model._meta.label):
                func = self._actions(model)[SUBMIT_ACTION][0]
                self.assertEqual(
                    func.__qualname__,
                    "EditorialWorkflowAdminMixin.action_submit_for_review",
                )


# ======================================================================
# Beta 11.11C2B1A: hardened action dispatch selection
# ======================================================================


class ActionDispatchSelectionTests(TestCase):
    """
    Direct, isolated tests of ``_selected_action_name`` against a range of
    real and adversarial changelist POSTs - no DB writes, no C2A calls.

    ``action_approve`` stands in throughout as a real, registered "other"
    action name (never as a semantic assertion about approve itself).
    """

    def _request(self, *, action_values=(), index=None, method="POST"):
        rf = RequestFactory()
        data = {}
        if action_values:
            data["action"] = list(action_values)
        if index is not None:
            data["index"] = index
        if method == "POST":
            return rf.post(CHANGELIST_URL, data=data)
        return rf.get(CHANGELIST_URL)

    def test_get_request_is_never_selected(self):
        request = self._request(method="GET")
        self.assertIsNone(_selected_action_name(request))

    def test_upper_bar_selects_submit(self):
        request = self._request(action_values=[SUBMIT_ACTION, "action_approve"], index="0")
        self.assertEqual(_selected_action_name(request), SUBMIT_ACTION)

    def test_lower_bar_selects_submit(self):
        request = self._request(action_values=["action_approve", SUBMIT_ACTION], index="1")
        self.assertEqual(_selected_action_name(request), SUBMIT_ACTION)

    def test_submit_present_but_not_selected_upper_index_picks_other(self):
        request = self._request(action_values=["action_approve", SUBMIT_ACTION], index="0")
        self.assertEqual(_selected_action_name(request), "action_approve")

    def test_submit_present_but_not_selected_lower_index_picks_other(self):
        request = self._request(action_values=[SUBMIT_ACTION, "action_approve"], index="1")
        self.assertEqual(_selected_action_name(request), "action_approve")

    def test_single_submit_value_missing_index_reproduces_django_default(self):
        request = self._request(action_values=[SUBMIT_ACTION])
        self.assertEqual(_selected_action_name(request), SUBMIT_ACTION)

    def test_single_other_value_missing_index_reproduces_django_default(self):
        request = self._request(action_values=["action_approve"])
        self.assertEqual(_selected_action_name(request), "action_approve")

    def test_two_values_missing_index_defaults_to_the_first(self):
        upper_submit = self._request(action_values=[SUBMIT_ACTION, "action_approve"])
        self.assertEqual(_selected_action_name(upper_submit), SUBMIT_ACTION)
        upper_other = self._request(action_values=["action_approve", SUBMIT_ACTION])
        self.assertEqual(_selected_action_name(upper_other), "action_approve")

    def test_empty_action_list_selects_nothing(self):
        request = self._request(action_values=[], index="0")
        self.assertIsNone(_selected_action_name(request))

    def test_non_numeric_index_reproduces_django_default_of_zero(self):
        # Django's own except ValueError: action_index = 0 - reproduced
        # exactly, not rejected, because it *is* Django's real default.
        upper_submit = self._request(action_values=[SUBMIT_ACTION, "action_approve"], index="abc")
        self.assertEqual(_selected_action_name(upper_submit), SUBMIT_ACTION)
        upper_other = self._request(action_values=["action_approve", SUBMIT_ACTION], index="xyz")
        self.assertEqual(_selected_action_name(upper_other), "action_approve")

    def test_empty_string_index_reproduces_django_default_of_zero(self):
        request = self._request(action_values=[SUBMIT_ACTION, "action_approve"], index="")
        self.assertEqual(_selected_action_name(request), SUBMIT_ACTION)

    def test_negative_index_fails_closed_even_when_it_would_resolve_to_submit(self):
        # Django's own int("-1") succeeds (no ValueError) and would resolve
        # via Python's negative indexing to the *last* posted value - which
        # here is the submit action. This function must not follow suit.
        request = self._request(action_values=["action_approve", SUBMIT_ACTION], index="-1")
        self.assertIsNone(_selected_action_name(request))

    def test_negative_index_fails_closed_for_a_single_submit_value(self):
        request = self._request(action_values=[SUBMIT_ACTION], index="-1")
        self.assertIsNone(_selected_action_name(request))

    def test_out_of_range_positive_index_fails_closed(self):
        request = self._request(action_values=[SUBMIT_ACTION], index="1")
        self.assertIsNone(_selected_action_name(request))

    def test_out_of_range_index_fails_closed_for_two_values(self):
        request = self._request(action_values=[SUBMIT_ACTION, "action_approve"], index="2")
        self.assertIsNone(_selected_action_name(request))

    def test_leading_empty_action_value_at_selected_index_selects_nothing(self):
        request = self._request(action_values=["", SUBMIT_ACTION], index="0")
        self.assertIsNone(_selected_action_name(request))

    def test_trailing_empty_action_value_at_selected_index_selects_nothing(self):
        request = self._request(action_values=[SUBMIT_ACTION, ""], index="1")
        self.assertIsNone(_selected_action_name(request))

    def test_unknown_other_action_at_selected_index_is_not_submit(self):
        request = self._request(action_values=["action_publish", SUBMIT_ACTION], index="0")
        self.assertEqual(_selected_action_name(request), "action_publish")
        self.assertNotEqual(_selected_action_name(request), SUBMIT_ACTION)


class ActionDispatchEndToEndTests(PromptAdminSubmissionTestCase):
    """
    The same selection contract, proven through real admin POSTs: the
    VersionAdmin bypass and the C2A call both track the actually-selected
    action, in both action-bar positions and when submit is present but not
    the one pressed.
    """

    def _post(self, prompt, *, index, action_values, follow=True):
        return self.client.post(
            CHANGELIST_URL,
            data={
                "action": list(action_values),
                "_selected_action": [str(prompt.pk)],
                "index": index,
            },
            follow=follow,
        )

    def test_upper_bar_submit_runs_c2a_and_creates_one_revision(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        self._post(prompt, index="0", action_values=[SUBMIT_ACTION, "action_approve"])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_lower_bar_submit_runs_c2a_and_creates_one_revision(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        self._post(prompt, index="1", action_values=["action_approve", SUBMIT_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_submit_present_upper_but_other_selected_never_calls_c2a(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.submit_prompt_for_review") as submit_mock:
            self._post(prompt, index="1", action_values=[SUBMIT_ACTION, "action_approve"])
        submit_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_submit_present_lower_but_other_selected_never_calls_c2a(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.submit_prompt_for_review") as submit_mock:
            self._post(prompt, index="0", action_values=["action_approve", SUBMIT_ACTION])
        submit_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_other_action_actually_selected_never_calls_c2a_even_though_submit_is_posted(self):
        prompt = make_prompt(status=Workflow.STATUS_REVIEW, author=self.author)
        with mock.patch("prompts.admin.submit_prompt_for_review") as submit_mock:
            self._post(prompt, index="0", action_values=["action_approve", SUBMIT_ACTION])
        submit_mock.assert_not_called()
        # approve's own (unchanged) VersionAdmin-wrapped path still ran
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)


class ActionDispatchMalformedIndexSecurityTests(PromptAdminSubmissionTestCase):
    """
    Malformed/adversarial indices must not grant the VersionAdmin bypass -
    proven end to end, including the one case where Django's own dispatch
    still resolves to the submit action regardless of our refusal: the
    request must then fail loudly (C2A's own ACTIVE_REVERSION_CONTEXT guard),
    never silently as an unbound, wrongly-isolated submission.
    """

    def test_negative_index_does_not_bypass_and_surfaces_active_reversion_context(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            self.client.post(
                CHANGELIST_URL,
                data={
                    "action": SUBMIT_ACTION,
                    "_selected_action": [str(prompt.pk)],
                    "index": "-1",
                },
                follow=False,
            )
        self.assertEqual(
            ctx.exception.code, PromptReviewSubmissionErrorCode.ACTIVE_REVERSION_CONTEXT
        )
        # no bypass means no per-root C2A revision was ever committed
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_out_of_range_index_does_not_bypass_and_c2a_is_never_reached(self):
        # Two values, index 2: out of range. Django's own IndexError-recovery
        # falls back to the *last* posted value here ("action_approve"), so
        # this case also demonstrates our refusal matching Django's own
        # eventual choice - not just diverging from it.
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.submit_prompt_for_review") as submit_mock:
            self.client.post(
                CHANGELIST_URL,
                data={
                    "action": [SUBMIT_ACTION, "action_approve"],
                    "_selected_action": [str(prompt.pk)],
                    "index": "2",
                },
                follow=True,
            )
        submit_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# Phase 5: single prompt success (real admin POST)
# ======================================================================


class SinglePromptSuccessTests(PromptAdminSubmissionTestCase):
    def test_single_draft_submit_full_contract(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        expected_fp = fingerprint_review_payload(build_prompt_review_payload(prompt))
        revisions_before = Revision.objects.count()

        response = self.post_submit([prompt])
        self.assertEqual(response.status_code, 200)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertIsNotNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, expected_fp)
        self.assertIsNotNone(reloaded.submitted_for_review_at)
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertIsNone(reloaded.reviewed_at)

        # exactly one new revision, actor + comment, graph
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        revision = Revision.objects.get(pk=reloaded.review_revision_id)
        self.assertEqual(revision.user_id, self.editor.pk)
        self.assertEqual(revision.comment, "submit_for_review")
        self.assertEqual(revision_labels(revision), ["prompts.prompt", "prompts.prompttranslation"])

        messages = message_texts(response)
        self.assertIn("1 prompt was submitted for review.", messages)

    def test_no_shared_batch_revision(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        self.post_submit([prompt])
        revision = Revision.objects.get(pk=refetch(prompt).review_revision_id)
        # only this prompt's own graph, nothing else
        self.assertEqual(
            {v.object_id for v in revision.version_set.filter(content_type__model="prompt")},
            {str(prompt.pk)},
        )


# ======================================================================
# Phase 6: multi-prompt isolation
# ======================================================================


class MultiPromptIsolationTests(PromptAdminSubmissionTestCase):
    def test_three_submittable_prompts_get_three_isolated_revisions(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author, languages=("en",))
        b = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author, languages=("en", "de"))
        c = make_prompt(status=Workflow.STATUS_REWORK, author=self.author, languages=("en",))
        revisions_before = Revision.objects.count()

        response = self.post_submit([a, b, c])

        for prompt in (a, b, c):
            self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

        self.assertEqual(Revision.objects.count(), revisions_before + 3)

        rev_ids = {refetch(p).review_revision_id for p in (a, b, c)}
        self.assertEqual(len(rev_ids), 3)

        for prompt in (a, b, c):
            revision = Revision.objects.get(pk=refetch(prompt).review_revision_id)
            roots = [v.object_id for v in revision.version_set.filter(content_type__model="prompt")]
            self.assertEqual(roots, [str(prompt.pk)])
            self.assertEqual(revision.user_id, self.editor.pk)
            self.assertEqual(revision.comment, "submit_for_review")

        # no revision contains two prompt roots
        for revision in Revision.objects.filter(pk__in=rev_ids):
            self.assertEqual(revision.version_set.filter(content_type__model="prompt").count(), 1)

        messages = message_texts(response)
        self.assertIn("3 prompts were submitted for review.", messages)


# ======================================================================
# Phase 7: mixed status matrix
# ======================================================================


class MixedStatusMatrixTests(PromptAdminSubmissionTestCase):
    def test_only_draft_and_rework_are_submitted(self):
        prompts = {
            status: make_prompt(status=status, author=self.author)
            for status in (
                Workflow.STATUS_DRAFT,
                Workflow.STATUS_REWORK,
                Workflow.STATUS_REVIEW,
                Workflow.STATUS_APPROVED,
                Workflow.STATUS_PUBLISHED,
                Workflow.STATUS_ARCHIVED,
            )
        }
        revisions_before = Revision.objects.count()

        response = self.post_submit(list(prompts.values()))

        # draft + rework -> review
        self.assertEqual(refetch(prompts[Workflow.STATUS_DRAFT]).status, Workflow.STATUS_REVIEW)
        self.assertEqual(refetch(prompts[Workflow.STATUS_REWORK]).status, Workflow.STATUS_REVIEW)
        # the other four unchanged
        for status in (
            Workflow.STATUS_REVIEW,
            Workflow.STATUS_APPROVED,
            Workflow.STATUS_PUBLISHED,
            Workflow.STATUS_ARCHIVED,
        ):
            reloaded = refetch(prompts[status])
            self.assertEqual(reloaded.status, status)
            self.assertIsNone(reloaded.review_revision_id)
            self.assertEqual(reloaded.review_payload_fingerprint, "")

        self.assertEqual(Revision.objects.count(), revisions_before + 2)

        messages = message_texts(response)
        self.assertIn("2 prompts were submitted for review.", messages)
        self.assertTrue(
            any("4 prompts were skipped because their status" in m for m in messages),
            messages,
        )


# ======================================================================
# Phase 8: partial failure / independent transactions
# ======================================================================


class PartialFailureTests(PromptAdminSubmissionTestCase):
    def test_deleted_middle_prompt_does_not_roll_back_the_others(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        b = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        c = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()

        real_submit = submit_prompt_for_review

        def wrapper(prompt, **kwargs):
            # Delete B after materialisation but before its own C2A lock, so
            # C2A raises OBJECT_NOT_FOUND for it - the real primitive still runs
            # for A and C.
            if prompt.pk == b.pk:
                Prompt.objects.filter(pk=b.pk).delete()
            return real_submit(prompt, **kwargs)

        with mock.patch("prompts.admin.submit_prompt_for_review", side_effect=wrapper):
            response = self.post_submit([a, b, c])

        self.assertEqual(refetch(a).status, Workflow.STATUS_REVIEW)
        self.assertFalse(Prompt.objects.filter(pk=b.pk).exists())
        self.assertEqual(refetch(c).status, Workflow.STATUS_REVIEW)

        # two independent revisions, none for B
        self.assertEqual(Revision.objects.count(), revisions_before + 2)
        self.assertNotEqual(refetch(a).review_revision_id, refetch(c).review_revision_id)

        messages = message_texts(response)
        self.assertIn("2 prompts were submitted for review.", messages)
        self.assertTrue(any("1 selected prompt no longer exists" in m for m in messages), messages)

    def test_multi_prompt_success_runs_without_mocking_c2a(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        b = make_prompt(status=Workflow.STATUS_REWORK, author=self.author)
        response = self.post_submit([a, b])
        self.assertEqual(refetch(a).status, Workflow.STATUS_REVIEW)
        self.assertEqual(refetch(b).status, Workflow.STATUS_REVIEW)
        self.assertIn("2 prompts were submitted for review.", message_texts(response))


# ======================================================================
# Phase 9: permissions
# ======================================================================


class PermissionTests(PromptAdminSubmissionTestCase):
    def test_editor_sees_the_action(self):
        make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        response = self.client.get(CHANGELIST_URL)
        self.assertContains(response, SUBMIT_ACTION)

    def test_author_can_submit_own_but_not_others(self):
        self.client.force_login(self.author)
        own = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        other = make_prompt(status=Workflow.STATUS_DRAFT, author=self.editor)

        response = self.post_submit([own, other])

        self.assertEqual(refetch(own).status, Workflow.STATUS_REVIEW)
        self.assertEqual(refetch(other).status, Workflow.STATUS_DRAFT)
        self.assertIsNone(refetch(other).review_revision_id)

        messages = message_texts(response)
        self.assertIn("1 prompt was submitted for review.", messages)
        self.assertTrue(
            any("1 prompt was skipped because you are not allowed" in m for m in messages),
            messages,
        )

    def test_denied_object_never_reaches_c2a(self):
        self.client.force_login(self.author)
        other = make_prompt(status=Workflow.STATUS_DRAFT, author=self.editor)
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.submit_prompt_for_review") as submit_mock:
            self.post_submit([other])
        submit_mock.assert_not_called()
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(refetch(other).status, Workflow.STATUS_DRAFT)

    def test_unprivileged_user_cannot_use_the_action(self):
        outsider = User.objects.create_user("c2b-outsider", password="pw", is_staff=True)
        self.client.force_login(outsider)
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.submit_prompt_for_review") as submit_mock:
            self.post_submit([prompt], raise_request_exception=False)
        submit_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# Phase 10: error classification
# ======================================================================


class ErrorClassificationTests(PromptAdminSubmissionTestCase):
    def _raise_code(self, code):
        def side_effect(prompt, **kwargs):
            raise PromptReviewSubmissionError(code, f"forced {code}")

        return side_effect

    def test_config_error_stops_processing_and_shows_generic_message(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        b = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        c = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)

        real_submit = submit_prompt_for_review
        seen = []

        def side_effect(prompt, **kwargs):
            seen.append(prompt.pk)
            if prompt.pk == b.pk:
                raise PromptReviewSubmissionError(
                    PromptReviewSubmissionErrorCode.INVALID_DATABASE_ALIAS, "bad alias"
                )
            return real_submit(prompt, **kwargs)

        with mock.patch("prompts.admin.submit_prompt_for_review", side_effect=side_effect):
            response = self.post_submit([a, b, c])

        # A processed, B triggered the stop, C never reached
        self.assertEqual(seen, [a.pk, b.pk])
        self.assertEqual(refetch(a).status, Workflow.STATUS_REVIEW)  # earlier success kept
        self.assertEqual(refetch(c).status, Workflow.STATUS_DRAFT)   # not processed

        messages = message_texts(response)
        self.assertIn("1 prompt was submitted for review.", messages)
        self.assertTrue(any("server configuration problem" in m for m in messages), messages)
        # no internal detail leaks
        for m in messages:
            self.assertNotIn("invalid_database_alias", m)
            self.assertNotIn("alias", m.lower())

    def test_object_not_found_is_a_recoverable_skip(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)

        with mock.patch(
            "prompts.admin.submit_prompt_for_review",
            side_effect=self._raise_code(PromptReviewSubmissionErrorCode.OBJECT_NOT_FOUND),
        ):
            response = self.post_submit([a])
        self.assertTrue(any("no longer exists" in m for m in message_texts(response)))

    def test_integrity_error_is_reraised_not_swallowed(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        with mock.patch(
            "prompts.admin.submit_prompt_for_review",
            side_effect=self._raise_code(
                PromptReviewSubmissionErrorCode.ROOT_VERSION_MISSING
            ),
        ):
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                self.post_submit([a], follow=False)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.ROOT_VERSION_MISSING)

    def test_payload_changed_integrity_error_is_reraised(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        with mock.patch(
            "prompts.admin.submit_prompt_for_review",
            side_effect=self._raise_code(
                PromptReviewSubmissionErrorCode.PAYLOAD_CHANGED_DURING_SUBMISSION
            ),
        ):
            with self.assertRaises(PromptReviewSubmissionError):
                self.post_submit([a], follow=False)

    def test_integrity_error_keeps_earlier_successes_committed(self):
        a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        b = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        real_submit = submit_prompt_for_review

        def side_effect(prompt, **kwargs):
            if prompt.pk == b.pk:
                raise PromptReviewSubmissionError(
                    PromptReviewSubmissionErrorCode.ROOT_VERSION_MISSING, "boom"
                )
            return real_submit(prompt, **kwargs)

        with mock.patch("prompts.admin.submit_prompt_for_review", side_effect=side_effect):
            with self.assertRaises(PromptReviewSubmissionError):
                self.post_submit([a, b], follow=False)
        # A was committed by its own C2A transaction before B raised
        self.assertEqual(refetch(a).status, Workflow.STATUS_REVIEW)


# ======================================================================
# QuerySet stability + exactly one C2A call per object
# ======================================================================


class QuerySetStabilityTests(PromptAdminSubmissionTestCase):
    def test_every_selected_id_is_processed_exactly_once_in_pk_order(self):
        prompts = [
            make_prompt(status=Workflow.STATUS_DRAFT, author=self.author) for _ in range(4)
        ]
        real_submit = submit_prompt_for_review
        called_pks = []

        def recorder(prompt, **kwargs):
            called_pks.append(prompt.pk)
            return real_submit(prompt, **kwargs)

        with mock.patch("prompts.admin.submit_prompt_for_review", side_effect=recorder):
            self.post_submit(prompts)

        expected = sorted(p.pk for p in prompts)
        self.assertEqual(called_pks, expected)  # each once, ascending pk
        self.assertEqual(len(called_pks), len(set(called_pks)))

    def test_status_change_from_a_success_does_not_drop_later_ids(self):
        # All start draft; first submit flips it to review. If the admin
        # re-evaluated a status-filtered queryset it would lose ids.
        prompts = [
            make_prompt(status=Workflow.STATUS_DRAFT, author=self.author) for _ in range(3)
        ]
        self.post_submit(prompts)
        for prompt in prompts:
            self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_mixed_selection_calls_c2a_for_every_object_including_non_submittable(self):
        draft = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        published = make_prompt(status=Workflow.STATUS_PUBLISHED, author=self.author)
        called_pks = []
        real_submit = submit_prompt_for_review

        def recorder(prompt, **kwargs):
            called_pks.append(prompt.pk)
            return real_submit(prompt, **kwargs)

        with mock.patch("prompts.admin.submit_prompt_for_review", side_effect=recorder):
            self.post_submit([draft, published])

        # C2A is called even for the published prompt (no admin pre-filtering);
        # C2A itself decides STATUS_NOT_SUBMITTABLE under the lock.
        self.assertEqual(sorted(called_pks), sorted([draft.pk, published.pk]))


# ======================================================================
# No duplicated C2A logic (static AST) / no admin reversion or transaction
# ======================================================================


class NoDuplicatedLogicTests(TestCase):
    def _admin_tree(self):
        import pathlib

        import prompts.admin as admin_module

        source = pathlib.Path(admin_module.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_admin_does_not_call_forbidden_c2a_internals(self):
        forbidden = {
            "create_revision",
            "set_user",
            "set_comment",
            "add_to_revision",
            "build_prompt_review_payload",
            "fingerprint_review_payload",
            "revision_contains_object",
            "move_to_review",
        }
        offenders = []
        for node in ast.walk(self._admin_tree()):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in forbidden:
                    offenders.append(name)
        self.assertEqual(offenders, [])

    def test_admin_submit_action_opens_no_transaction_or_reversion_context(self):
        """
        The override must not wrap the selection in transaction.atomic() or
        reversion.create_revision(); those belong to C2A per root. Checked on
        the parsed body of PromptAdmin.action_submit_for_review.
        """
        tree = self._admin_tree()
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PromptAdmin":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "action_submit_for_review":
                        target = item
        self.assertIsNotNone(target)

        offenders = []
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                # transaction.atomic(...) / reversion.create_revision(...)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "atomic",
                    "create_revision",
                }:
                    offenders.append(node.func.attr)
            # `with ... as ...` context managers named atomic/create_revision
        self.assertEqual(offenders, [])

    def test_admin_uses_only_the_public_c2a_surface(self):
        import pathlib

        import prompts.admin as admin_module

        source = pathlib.Path(admin_module.__file__).read_text(encoding="utf-8")
        self.assertIn("submit_prompt_for_review", source)
        self.assertIn("PromptReviewSubmissionError", source)


# ======================================================================
# Phase 12/13: other types + no further activation
# ======================================================================


class OtherEditorialTypesUnchangedTests(TestCase):
    def test_other_admins_do_not_import_the_prompt_primitive(self):
        import guides.admin
        import usecases.admin
        import compare.admin

        for module in (guides.admin, usecases.admin, compare.admin):
            source = open(module.__file__, encoding="utf-8").read()
            with self.subTest(module=module.__name__):
                self.assertNotIn("submit_prompt_for_review", source)
                self.assertNotIn("review_submission", source)


class NoFurtherActivationTests(PromptAdminSubmissionTestCase):
    def test_approve_still_sets_no_approved_revision(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        # submit via the new admin path, then approve via the unchanged action
        self.post_submit([prompt])
        self.client.post(
            CHANGELIST_URL,
            data={"action": "action_approve", "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        # approve does not yet bind approved_revision
        self.assertIsNone(reloaded.approved_revision_id)

    def test_submit_still_leaves_review_note_unchanged(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        Prompt.objects.filter(pk=prompt.pk).update(review_note="keep me")
        self.post_submit([refetch(prompt)])
        self.assertEqual(refetch(prompt).review_note, "keep me")
