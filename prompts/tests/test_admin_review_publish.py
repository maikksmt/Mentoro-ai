"""
Beta 11.11D2: both prompt publish routes, now backed by the one per-root
publish primitive.

Until D2 a prompt could be published two ways and they did not agree. The
shared admin action batched a whole changelist selection into one transaction
and one revision and then asked ``set_last_published_revision()`` for a marker
it could not resolve correctly; the editorial view called the FSM transition
plus ``obj.save()`` with no transaction, no revision and no marker at all.
Neither validated the binding or the fingerprint it was publishing.

This module proves both routes now delegate to ``publish_prompt_review`` -
one atomic transaction, one revision, one correct marker, one binding check
per prompt - while preserving each route's name, description, permission
contract and UX, and without opening any transaction or reversion context of
their own. Guide/UseCase/Comparison keep the shared path untouched.

D2 also extends the Beta 11.11C2B1A ``_selected_action_name``-based
VersionAdmin bypass to cover publishing alongside submission and approval;
that contract is reused verbatim, never reimplemented.
"""
import ast
import itertools
import pathlib
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from reversion.models import Revision, Version

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide
from prompts.models import Prompt, PromptTranslation
from prompts.review_approval import approve_prompt_review
from prompts.review_publish import (
    PromptReviewPublishError,
    PromptReviewPublishErrorCode,
    publish_prompt_review,
)
from prompts.review_submission import submit_prompt_for_review
from usecases.models import UseCase

User = get_user_model()

_counter = itertools.count()

PUBLISH_ACTION = "action_publish"
APPROVE_ACTION = "action_approve"
SUBMIT_ACTION = "action_submit_for_review"
CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")


def refetch(prompt):
    return Prompt.objects.get(pk=prompt.pk)


def message_texts(response):
    return [str(m) for m in response.context["messages"]]


class PromptAdminPublishTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(
            "d2b-editor", password="pw", is_staff=True, first_name="Ed", last_name="Itor"
        )
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user(
            "d2b-author", password="pw", is_staff=True, first_name="Au", last_name="Thor"
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.other_author = User.objects.create_user(
            "d2b-author-2", password="pw", is_staff=True
        )
        cls.other_author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)

    # -- construction --------------------------------------------------

    def make_prompt(self, *, author=None, languages=("en",)):
        prompt = Prompt.objects.create(
            status=Workflow.STATUS_DRAFT, author=author or self.author
        )
        for language_code in languages:
            prompt.create_translation(
                language_code,
                title=f"Title {language_code}",
                intro="intro",
                body="body",
                outro="outro",
                slug=f"d2b-{language_code}-{next(_counter)}",
            )
        return prompt

    def approved(self, *, author=None, languages=("en",)):
        prompt = self.make_prompt(author=author, languages=languages)
        submit_prompt_for_review(refetch(prompt), actor=self.author)
        approve_prompt_review(refetch(prompt), actor=self.editor)
        return refetch(prompt)

    # -- the two routes ------------------------------------------------

    def post_publish(self, prompts, *, index="0", action_values=None, follow=True, client=None):
        data = {
            "_selected_action": [str(p.pk) for p in prompts],
            "index": index,
        }
        data["action"] = (
            [PUBLISH_ACTION] if action_values is None else list(action_values)
        )
        return (client or self.client).post(CHANGELIST_URL, data=data, follow=follow)

    def tolerant_client(self, user=None):
        """A logged-in client that returns the 500 instead of re-raising, for
        the cases where D2's fail-closed refusal is the expected outcome."""
        client = Client(raise_request_exception=False)
        client.force_login(user or self.editor)
        return client

    def post_view_publish(self, prompt, *, url_name, client=None):
        return (client or self.client).post(
            reverse(f"content:editorial:{url_name}"),
            data={"model": "prompt", "object_id": str(prompt.pk), "status": "published"},
            follow=True,
        )


# ======================================================================
# Action identity / registration
# ======================================================================


class ActionRegistrationTests(PromptAdminPublishTestCase):
    def _actions(self, model):
        model_admin = django_admin.site._registry[model]
        return model_admin.get_actions(
            self.client.get(CHANGELIST_URL).wsgi_request
        )

    def test_prompt_admin_has_exactly_one_publish_action(self):
        names = [n for n in self._actions(Prompt) if n == PUBLISH_ACTION]
        self.assertEqual(names, [PUBLISH_ACTION])

    def test_prompt_publish_action_resolves_to_the_override(self):
        from core.admin import EditorialWorkflowAdminMixin

        func = self._actions(Prompt)[PUBLISH_ACTION][0]
        prompt_admin = django_admin.site._registry[Prompt]
        self.assertIs(func, type(prompt_admin).action_publish)
        self.assertIsNot(func, EditorialWorkflowAdminMixin.action_publish)

    def test_prompt_publish_action_keeps_a_description(self):
        description = self._actions(Prompt)[PUBLISH_ACTION][2]
        self.assertIn("publish", str(description).lower())

    def test_submit_and_approve_actions_still_exist(self):
        actions = self._actions(Prompt)
        self.assertIn(SUBMIT_ACTION, actions)
        self.assertIn(APPROVE_ACTION, actions)

    def test_other_editorial_types_keep_the_shared_publish_action(self):
        from core.admin import EditorialWorkflowAdminMixin

        for model in (Guide, UseCase, Comparison):
            with self.subTest(model=model.__name__):
                model_admin = django_admin.site._registry[model]
                self.assertIs(
                    type(model_admin).action_publish,
                    EditorialWorkflowAdminMixin.action_publish,
                )


# ======================================================================
# VersionAdmin bypass (C2B1A contract, extended)
# ======================================================================


class ActionDispatchPublishTests(PromptAdminPublishTestCase):
    def test_publish_selected_upper_bar_is_bypassed_and_succeeds(self):
        prompt = self.approved()
        self.post_publish([prompt], index="0", action_values=[PUBLISH_ACTION, APPROVE_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_PUBLISHED)

    def test_publish_selected_lower_bar_is_bypassed_and_succeeds(self):
        prompt = self.approved()
        self.post_publish([prompt], index="1", action_values=[APPROVE_ACTION, PUBLISH_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_PUBLISHED)

    def test_publish_present_but_approve_selected_runs_approve_not_publish(self):
        prompt = self.make_prompt()
        submit_prompt_for_review(refetch(prompt), actor=self.author)
        self.post_publish([prompt], index="0", action_values=[APPROVE_ACTION, PUBLISH_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

    def test_publish_present_but_other_action_selected_never_calls_d2(self):
        prompt = self.approved()
        with mock.patch("prompts.admin.publish_prompt_review") as primitive:
            self.post_publish(
                [prompt], index="0", action_values=["action_archive", PUBLISH_ACTION]
            )
        primitive.assert_not_called()

    def test_out_of_range_index_never_grants_the_bypass(self):
        """
        Unchanged C2B1A contract, now covering publish: a crafted index fails
        closed in ``_selected_action_name`` (returning ``None``), so the
        request keeps VersionAdmin's revision context. Django's own dispatch
        may still select the publish action from such a POST - and when it
        does, it runs *inside* that retained revision context, where D2's own
        ``ACTIVE_REVERSION_CONTEXT`` check refuses it. Either way the prompt
        is not published; the bypass is never granted by the index alone.
        """
        from prompts.admin import _selected_action_name

        prompt = self.approved()
        request = self.tolerant_client().post(
            CHANGELIST_URL,
            data={
                "action": PUBLISH_ACTION,
                "_selected_action": [str(prompt.pk)],
                "index": "99",
            },
        ).wsgi_request
        self.assertIsNone(_selected_action_name(request))
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertFalse(refetch(prompt).is_published)

    def test_negative_index_never_grants_the_bypass(self):
        prompt = self.approved()
        self.tolerant_client().post(
            CHANGELIST_URL,
            data={
                "action": [PUBLISH_ACTION, APPROVE_ACTION],
                "_selected_action": [str(prompt.pk)],
                "index": "-1",
            },
        )
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertFalse(refetch(prompt).is_published)


# ======================================================================
# Single publish: the full contract through the admin
# ======================================================================


class SinglePublishContractTests(PromptAdminPublishTestCase):
    def test_single_publish_full_contract(self):
        prompt = self.approved(languages=("en", "de"))
        before = refetch(prompt)
        revisions_before = Revision.objects.count()

        self.post_publish([prompt])

        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(after.is_published)
        self.assertIsNotNone(after.published_at)
        self.assertEqual(set(after.live_i18n), {"en", "de"})
        self.assertEqual(after.live_author["display_name"], "Au Thor")
        # bindings and fingerprint survive untouched
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.approved_revision_id, before.approved_revision_id)
        self.assertEqual(
            after.review_payload_fingerprint, before.review_payload_fingerprint
        )
        # exactly one new revision, and the marker points into it
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        marker = Version.objects.get(pk=after.last_published_revision_id)
        self.assertEqual(marker.content_type.model, "prompt")
        self.assertEqual(marker.object_id, str(prompt.pk))
        self.assertEqual(marker.revision_id, Revision.objects.order_by("-pk").first().pk)

    def test_marker_is_not_the_approval_version(self):
        """The defect D2 fixes: ``set_last_published_revision()`` resolved the
        marker inside the still-open revision block with an unordered
        ``.first()``, so it could only ever point at an older version."""
        prompt = self.approved()
        approval_versions = set(
            Version.objects.filter(
                content_type__app_label="prompts",
                content_type__model="prompt",
                object_id=str(prompt.pk),
            ).values_list("pk", flat=True)
        )
        self.post_publish([prompt])
        self.assertNotIn(refetch(prompt).last_published_revision_id, approval_versions)

    def test_public_slug_and_snapshot_agree(self):
        """The second defect D2 fixes: ``_update_live_snapshot()`` ran before
        ``on_after_publish()`` set ``public_slug``, so the snapshot froze the
        pre-publish value while the row got the new one."""
        prompt = self.approved(languages=("en", "de"))
        self.post_publish([prompt])
        after = refetch(prompt)
        for language_code in ("en", "de"):
            translation = PromptTranslation.objects.get(
                master_id=after.pk, language_code=language_code
            )
            self.assertIsNotNone(translation.public_slug)
            self.assertEqual(translation.public_slug, translation.slug)
            self.assertEqual(
                translation.public_slug, after.live_i18n[language_code]["public_slug"]
            )

    def test_review_note_is_not_overwritten(self):
        """The shared action passed ``note="Admin-Action publish"`` into the
        transition, clobbering the editor's review note. D2 passes none."""
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(review_note="keep me")
        self.post_publish([refetch(prompt)])
        self.assertEqual(refetch(prompt).review_note, "keep me")

    def test_no_shared_batch_revision(self):
        prompts = [self.approved() for _ in range(3)]
        revisions_before = Revision.objects.count()
        self.post_publish(prompts)
        self.assertEqual(Revision.objects.count(), revisions_before + 3)

    def test_three_prompts_get_three_isolated_revisions_and_markers(self):
        prompts = [self.approved() for _ in range(3)]
        self.post_publish(prompts)

        markers = []
        for prompt in prompts:
            after = refetch(prompt)
            self.assertEqual(after.status, Workflow.STATUS_PUBLISHED)
            marker = Version.objects.get(pk=after.last_published_revision_id)
            self.assertEqual(marker.object_id, str(prompt.pk))
            markers.append(marker.revision_id)
        self.assertEqual(len(set(markers)), 3)


# ======================================================================
# Mixed selections and recoverable skips
# ======================================================================


class MixedSelectionTests(PromptAdminPublishTestCase):
    def test_only_the_approved_prompt_is_published(self):
        draft = self.make_prompt()
        in_review = self.make_prompt()
        submit_prompt_for_review(refetch(in_review), actor=self.author)
        approved = self.approved()

        response = self.post_publish([draft, in_review, approved])

        self.assertEqual(refetch(draft).status, Workflow.STATUS_DRAFT)
        self.assertEqual(refetch(in_review).status, Workflow.STATUS_REVIEW)
        self.assertEqual(refetch(approved).status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(
            any("status does not allow" in t for t in message_texts(response))
        )

    def test_stale_approved_content_is_skipped_not_published(self):
        prompt = self.approved()
        PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Changed")
        response = self.post_publish([prompt])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertFalse(refetch(prompt).is_published)
        self.assertTrue(any("has changed" in t for t in message_texts(response)))

    def test_a_prompt_without_a_complete_translation_is_skipped(self):
        prompt = self.approved()
        other = self.approved()
        PromptTranslation.objects.filter(master_id=prompt.pk).update(title="  ")
        # re-approve the new payload so only completeness can fail
        from core.review_binding import fingerprint_review_payload
        from prompts.review_payload import build_prompt_review_payload

        Prompt.objects.filter(pk=prompt.pk).update(
            review_payload_fingerprint=fingerprint_review_payload(
                build_prompt_review_payload(refetch(prompt))
            )
        )

        response = self.post_publish([prompt, other])

        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertEqual(refetch(other).status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(
            any("complete translation" in t for t in message_texts(response))
        )

    def test_a_deleted_prompt_does_not_roll_back_the_others(self):
        first = self.approved()
        doomed = self.approved()
        last = self.approved()
        Prompt.objects.filter(pk=doomed.pk).delete()

        self.post_publish([first, doomed, last])

        self.assertEqual(refetch(first).status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(refetch(last).status, Workflow.STATUS_PUBLISHED)

    def test_every_selected_id_is_processed_in_pk_order(self):
        prompts = [self.approved() for _ in range(3)]
        seen = []
        real = publish_prompt_review

        def spy(prompt, **kwargs):
            seen.append(prompt.pk)
            return real(prompt, **kwargs)

        with mock.patch("prompts.admin.publish_prompt_review", side_effect=spy):
            self.post_publish(list(reversed(prompts)))
        self.assertEqual(seen, sorted(p.pk for p in prompts))


# ======================================================================
# Integrity failures are never harmless skips
# ======================================================================


class IntegrityFailureTests(PromptAdminPublishTestCase):
    def test_corrupt_binding_is_reraised_not_skipped(self):
        prompt = self.approved()
        other = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(
            approved_revision_id=other.review_revision_id
        )
        with self.assertRaises(PromptReviewPublishError) as ctx:
            self.post_publish([prompt], follow=False)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.APPROVED_BINDING_INVALID
        )
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

    def test_postcondition_failure_is_reraised(self):
        prompt = self.approved()
        with mock.patch(
            "prompts.review_publish._verify_publish_postconditions",
            side_effect=PromptReviewPublishError(
                PromptReviewPublishErrorCode.PUBLISH_POSTCONDITION_FAILED, "forced"
            ),
        ):
            with self.assertRaises(PromptReviewPublishError):
                self.post_publish([prompt], follow=False)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

    def test_config_error_stops_processing_and_keeps_earlier_successes(self):
        first = self.approved()
        second = self.approved()
        real = publish_prompt_review
        calls = {"n": 0}

        def flaky(prompt, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise PromptReviewPublishError(
                    PromptReviewPublishErrorCode.INVALID_ACTOR, "forced"
                )
            return real(prompt, **kwargs)

        with mock.patch("prompts.admin.publish_prompt_review", side_effect=flaky):
            response = self.post_publish([first, second])

        self.assertEqual(refetch(first).status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(refetch(second).status, Workflow.STATUS_APPROVED)
        texts = " ".join(message_texts(response))
        self.assertIn("configuration problem", texts)
        # no internal code, alias or id leaks into the admin UI
        self.assertNotIn("invalid_actor", texts)


# ======================================================================
# Permissions
# ======================================================================


class PublishPermissionTests(PromptAdminPublishTestCase):
    def test_author_can_publish_their_own_approved_prompt(self):
        prompt = self.approved(author=self.author)
        client = Client()
        client.force_login(self.author)
        self.post_publish([prompt], client=client)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_PUBLISHED)

    def test_author_cannot_publish_someone_elses_prompt(self):
        prompt = self.approved(author=self.author)
        client = Client()
        client.force_login(self.other_author)
        response = self.post_publish([prompt], client=client)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertTrue(any("not allowed" in t for t in message_texts(response)))

    def test_denied_object_never_reaches_the_primitive(self):
        prompt = self.approved(author=self.author)
        client = Client()
        client.force_login(self.other_author)
        with mock.patch("prompts.admin.publish_prompt_review") as primitive:
            self.post_publish([prompt], client=client)
        primitive.assert_not_called()


# ======================================================================
# The editorial views (both of them)
# ======================================================================


class EditorialViewPublishTests(PromptAdminPublishTestCase):
    def test_my_content_update_publishes_through_the_primitive(self):
        prompt = self.approved(author=self.author)
        client = Client()
        client.force_login(self.author)
        with mock.patch(
            "content.views.editorial.publish_prompt_review",
            side_effect=publish_prompt_review,
        ) as primitive:
            self.post_view_publish(prompt, url_name="my_content_update", client=client)
        primitive.assert_called_once()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_PUBLISHED)

    def test_review_update_publishes_through_the_primitive(self):
        prompt = self.approved(author=self.author)
        with mock.patch(
            "content.views.editorial.publish_prompt_review",
            side_effect=publish_prompt_review,
        ) as primitive:
            self.post_view_publish(prompt, url_name="review_update")
        primitive.assert_called_once()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_PUBLISHED)

    def test_the_view_route_now_writes_a_revision_and_a_marker(self):
        """Before D2 this route opened no reversion context and wrote no
        marker at all."""
        prompt = self.approved(author=self.author)
        revisions_before = Revision.objects.count()
        self.post_view_publish(prompt, url_name="review_update")
        after = refetch(prompt)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertIsNotNone(after.last_published_revision_id)
        marker = Version.objects.get(pk=after.last_published_revision_id)
        self.assertEqual(marker.object_id, str(prompt.pk))

    def test_the_view_route_refuses_a_stale_approved_payload(self):
        """Before D2 this route published whatever was on disk, with no
        fingerprint check whatsoever."""
        prompt = self.approved(author=self.author)
        PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Changed")
        response = self.post_view_publish(prompt, url_name="review_update")
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertFalse(refetch(prompt).is_published)
        self.assertTrue(any("has changed" in t for t in message_texts(response)))

    def test_the_view_route_refuses_a_non_approved_prompt(self):
        prompt = self.make_prompt(author=self.author)
        response = self.post_view_publish(prompt, url_name="review_update")
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertTrue(
            any("not allowed from current state" in t for t in message_texts(response))
        )

    def test_other_editorial_types_still_use_the_generic_view_path(self):
        guide = Guide.objects.create(status=Workflow.STATUS_APPROVED, author=self.author)
        guide.create_translation("en", title="G", slug=f"d2b-guide-{next(_counter)}")
        with mock.patch("content.views.editorial.publish_prompt_review") as primitive:
            self.client.post(
                reverse("content:editorial:review_update"),
                data={"model": "guide", "object_id": str(guide.pk), "status": "published"},
                follow=True,
            )
        primitive.assert_not_called()
        self.assertEqual(
            Guide.objects.get(pk=guide.pk).status, Workflow.STATUS_PUBLISHED
        )


# ======================================================================
# D1 compatibility: what the publish makes publicly visible
# ======================================================================


class D1VisibilityCompatibilityTests(PromptAdminPublishTestCase):
    def test_a_published_prompt_is_visible_in_its_language_only(self):
        prompt = self.approved(languages=("en",))
        self.post_publish([prompt])
        self.assertIn(
            prompt.pk,
            list(Prompt.objects.visible_in_language("en").values_list("pk", flat=True)),
        )
        self.assertNotIn(
            prompt.pk,
            list(Prompt.objects.visible_in_language("de").values_list("pk", flat=True)),
        )

    def test_a_published_prompt_is_in_visible_on_site(self):
        prompt = self.approved()
        self.post_publish([prompt])
        self.assertIn(
            prompt.pk,
            list(Prompt.objects.visible_on_site().values_list("pk", flat=True)),
        )

    def test_a_refused_publish_leaves_the_prompt_invisible(self):
        prompt = self.approved()
        PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Changed")
        self.post_publish([prompt])
        self.assertNotIn(
            prompt.pk,
            list(Prompt.objects.visible_on_site().values_list("pk", flat=True)),
        )


# ======================================================================
# No duplicated logic (AST, scoped)
# ======================================================================


class NoDuplicatedLogicTests(TestCase):
    def _function(self, module, class_name, function_name):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if class_name is None:
                if isinstance(node, ast.FunctionDef) and node.name == function_name:
                    return node
                continue
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == function_name:
                        return item
        return None

    def _forbidden_calls(self, target, forbidden):
        offenders = []
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in forbidden:
                    offenders.append(name)
        return offenders

    #: Everything the admin action and the view helper must leave entirely to
    #: the primitive. ``publish`` is the FSM transition itself.
    FORBIDDEN = {
        "atomic",
        "create_revision",
        "set_user",
        "set_comment",
        "add_to_revision",
        "build_prompt_review_payload",
        "fingerprint_review_payload",
        "validate_review_binding",
        "validate_approved_binding",
        "revision_contains_object",
        "invalidate_editorial_review_state",
        "set_last_published_revision",
        "publish",
    }

    def test_admin_publish_action_duplicates_nothing(self):
        """
        Scoped to ``PromptAdmin.action_publish`` itself, not the whole
        ``prompts/admin.py`` file: Beta 11.11C4J legitimately calls several of
        these same primitives elsewhere in that module, for the unrelated
        purpose of fail-closed invalidating a binding a reversion
        revert/recover restored.
        """
        import prompts.admin as admin_module

        target = self._function(admin_module, "PromptAdmin", "action_publish")
        self.assertIsNotNone(target)
        self.assertEqual(self._forbidden_calls(target, self.FORBIDDEN), [])

    def test_view_publish_helper_duplicates_nothing(self):
        import content.views.editorial as editorial_module

        target = self._function(
            editorial_module, None, "_publish_prompt_review_via_primitive"
        )
        self.assertIsNotNone(target)
        self.assertEqual(self._forbidden_calls(target, self.FORBIDDEN), [])

    def test_both_routes_use_only_the_public_d2_surface(self):
        import content.views.editorial as editorial_module
        import prompts.admin as admin_module

        for module in (admin_module, editorial_module):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            with self.subTest(module=module.__name__):
                self.assertIn("publish_prompt_review", source)
                self.assertIn("PromptReviewPublishError", source)
                self.assertNotIn("_verify_publish_postconditions", source)
                self.assertNotIn("_resolve_root_version", source)
                self.assertNotIn("_store_publish_marker", source)


class NoRuntimeActivationTests(TestCase):
    def test_only_the_two_sanctioned_consumers_exist(self):
        """
        D2's public surface is consumed by exactly two production modules -
        ``prompts/admin.py`` and ``content/views/editorial.py`` - and defined
        in exactly one. Mirrors the equivalent C2A/C3A allow-list tests.
        """
        import content.views.editorial as editorial_views_module
        import prompts.admin as admin_module
        import prompts.review_publish as publish_module

        symbols = (
            "publish_prompt_review",
            "PromptReviewPublishResult",
            "PromptReviewPublishError",
            "PromptReviewPublishErrorCode",
        )
        definition_file = pathlib.Path(publish_module.__file__).resolve()
        allowed_files = {
            definition_file,
            pathlib.Path(admin_module.__file__).resolve(),
            pathlib.Path(editorial_views_module.__file__).resolve(),
        }
        project_root = definition_file.parents[1]

        offenders = []
        for py_file in project_root.rglob("*.py"):
            if "venv" in py_file.parts or "migrations" in py_file.parts:
                continue
            if "/tests/" in str(py_file) or py_file.name.startswith("test_"):
                continue
            if py_file.resolve() in allowed_files:
                continue
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if not any(symbol in text for symbol in symbols):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = getattr(node, "module", None) or ""
                    names = [a.name for a in node.names]
                    if "review_publish" in module_name or any(s in names for s in symbols):
                        offenders.append(f"{py_file}: import")
                if isinstance(node, ast.Name) and node.id in symbols:
                    offenders.append(f"{py_file}: reference {node.id}")
        self.assertEqual(offenders, [])

    def test_no_other_production_module_calls_the_prompt_fsm_publish(self):
        """
        Nothing outside the primitive may call ``Prompt.publish()`` directly.
        The shared ``EditorialWorkflowAdminMixin.action_publish`` and the
        generic editorial-view dispatch still do so for Guide/UseCase/
        Comparison - that is deliberate and out of D2's scope - but neither
        may reach a Prompt any more, which the runtime tests above pin.
        """
        import prompts.review_publish as publish_module

        definition_file = pathlib.Path(publish_module.__file__).resolve()
        prompts_package = definition_file.parent

        offenders = []
        for py_file in prompts_package.rglob("*.py"):
            if "migrations" in py_file.parts or "/tests/" in str(py_file):
                continue
            if py_file.resolve() == definition_file:
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "publish"
                ):
                    offenders.append(f"{py_file.name}:{node.lineno}")
        self.assertEqual(offenders, [])


class OtherEditorialTypesUnchangedTests(TestCase):
    def test_other_admins_do_not_import_the_publish_primitive(self):
        import compare.admin
        import guides.admin
        import usecases.admin

        for module in (guides.admin, usecases.admin, compare.admin):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            with self.subTest(module=module.__name__):
                self.assertNotIn("publish_prompt_review", source)
                self.assertNotIn("review_publish", source)
