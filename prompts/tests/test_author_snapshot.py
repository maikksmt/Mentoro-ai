"""
Beta 11.11C4E: the immutable, global publish-time author display snapshot.

``Prompt.live_author`` answers exactly one question for a future rendering
slice: "what public author name should this published page show?" - frozen at
the moment of a conscious publish/republish, never refreshed by a later
account change. These tests hold ``Prompt._build_live_author_snapshot()`` and
its wiring into ``on_after_publish()`` to that contract: first publish creates
the snapshot, name/username/author/account-deletion changes afterwards never
touch it, a conscious republish replaces it wholesale, both real production
publish paths (admin action, editorial view) produce the identical snapshot
through the same single code path, no extra save or reversion context is
introduced, and the review payload/fingerprint contract (Beta 11.11C4D)
remains completely unaffected.
"""
import itertools
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from reversion.models import Revision

from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import fingerprint_review_payload
from guides.models import Guide
from compare.models import Comparison
from usecases.models import UseCase
from prompts.models import PROMPT_AUTHOR_SNAPSHOT_SCHEMA, Prompt
from prompts.review_payload import build_prompt_review_payload

User = get_user_model()

_slug_counter = itertools.count()

CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")


def refetch(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


def make_prompt(*, status=Workflow.STATUS_DRAFT, author=None, languages=("en",)):
    prompt = Prompt.objects.create(status=status, author=author)
    for language_code in languages:
        prompt.create_translation(
            language_code,
            title=f"Title {language_code}",
            intro="intro",
            body="body",
            outro="outro",
            slug=f"snap-slug-{next(_slug_counter)}",
        )
    return prompt


def publish_directly(prompt, *, by):
    """The real FSM transition plus the same full save every production
    publish path performs - never a shortcut that assigns ``live_author``
    itself."""
    prompt.publish(by=by)
    prompt.save()
    return refetch(prompt)


def full_cycle_to_published(prompt, *, actor):
    """Draft -> review -> approved -> published, entirely through the real
    FSM transitions - the only way this module ever reaches "published"."""
    if prompt.status not in (Workflow.STATUS_REVIEW, Workflow.STATUS_APPROVED, Workflow.STATUS_PUBLISHED):
        prompt.move_to_review(by=actor)
        prompt.save()
    prompt = refetch(prompt)
    if prompt.status == Workflow.STATUS_REVIEW:
        prompt.approve(by=actor)
        prompt.save()
    prompt = refetch(prompt)
    return publish_directly(prompt, by=actor)


class SnapshotBuilderTests(TestCase):
    """Direct, pure tests of ``_build_live_author_snapshot()`` - never saved,
    never mutates anything beyond the returned dict."""

    def test_no_author_yields_explicit_empty_display_name(self):
        prompt = make_prompt(author=None)
        snapshot = prompt._build_live_author_snapshot()
        self.assertEqual(snapshot, {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": ""})

    def test_full_name_is_used_when_present(self):
        author = User.objects.create_user(
            "snap-fullname", password="pw", first_name="Ada", last_name="Lovelace"
        )
        prompt = make_prompt(author=author)
        snapshot = prompt._build_live_author_snapshot()
        self.assertEqual(snapshot["display_name"], "Ada Lovelace")

    def test_username_fallback_when_no_name_is_set(self):
        author = User.objects.create_user("snap-username-only", password="pw")
        prompt = make_prompt(author=author)
        snapshot = prompt._build_live_author_snapshot()
        self.assertEqual(snapshot["display_name"], "snap-username-only")

    def test_schema_is_exactly_the_documented_constant(self):
        prompt = make_prompt(author=None)
        snapshot = prompt._build_live_author_snapshot()
        self.assertEqual(snapshot["schema"], "prompt-author-v1")
        self.assertEqual(PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "prompt-author-v1")

    def test_snapshot_shape_is_exactly_two_keys(self):
        author = User.objects.create_user("snap-shape", password="pw", first_name="A")
        prompt = make_prompt(author=author)
        self.assertEqual(set(prompt._build_live_author_snapshot()), {"schema", "display_name"})

    def test_builder_does_not_mutate_or_save_the_object(self):
        author = User.objects.create_user("snap-no-mutate", password="pw")
        prompt = make_prompt(author=author)
        before = refetch(prompt).live_author
        prompt._build_live_author_snapshot()
        self.assertEqual(prompt.live_author, before)
        self.assertEqual(refetch(prompt).live_author, before)

    def test_builder_is_deterministic_for_the_same_state(self):
        author = User.objects.create_user("snap-deterministic", password="pw", first_name="Grace")
        prompt = make_prompt(author=author)
        self.assertEqual(
            prompt._build_live_author_snapshot(), prompt._build_live_author_snapshot()
        )


class FirstPublishTests(TestCase):
    def test_live_author_is_none_before_any_publish(self):
        author = User.objects.create_user("snap-before-pub", password="pw")
        prompt = make_prompt(author=author)
        self.assertIsNone(refetch(prompt).live_author)

    def test_first_publish_creates_the_snapshot(self):
        author = User.objects.create_user(
            "snap-first-pub", password="pw", first_name="Jane", last_name="Doe"
        )
        prompt = full_cycle_to_published(make_prompt(author=author), actor=author)
        self.assertEqual(
            prompt.live_author, {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Jane Doe"}
        )

    def test_first_publish_leaves_the_existing_status_and_content_contract_intact(self):
        author = User.objects.create_user("snap-first-pub-contract", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=author), actor=author)
        self.assertEqual(prompt.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(prompt.is_published)
        self.assertIsNotNone(prompt.published_at)
        self.assertIn("en", prompt.live_i18n)


class StabilityMatrixTests(TestCase):
    """After a successful publish, none of these ever change ``live_author``
    on their own - only a conscious republish may."""

    def _published_prompt(self, *, author):
        return full_cycle_to_published(make_prompt(author=author), actor=author)

    def test_first_name_change_does_not_affect_the_snapshot(self):
        author = User.objects.create_user("snap-stable-first", password="pw", first_name="Old")
        prompt = self._published_prompt(author=author)
        before = prompt.live_author
        User.objects.filter(pk=author.pk).update(first_name="New")
        self.assertEqual(refetch(prompt).live_author, before)

    def test_last_name_change_does_not_affect_the_snapshot(self):
        author = User.objects.create_user("snap-stable-last", password="pw", last_name="Old")
        prompt = self._published_prompt(author=author)
        before = prompt.live_author
        User.objects.filter(pk=author.pk).update(last_name="New")
        self.assertEqual(refetch(prompt).live_author, before)

    def test_username_change_does_not_affect_the_snapshot(self):
        author = User.objects.create_user("snap-stable-username-old", password="pw")
        prompt = self._published_prompt(author=author)
        before = prompt.live_author
        User.objects.filter(pk=author.pk).update(username="snap-stable-username-new")
        self.assertEqual(refetch(prompt).live_author, before)

    def test_author_reassignment_does_not_affect_the_snapshot(self):
        author_a = User.objects.create_user("snap-stable-author-a", password="pw", first_name="A")
        author_b = User.objects.create_user("snap-stable-author-b", password="pw", first_name="B")
        prompt = self._published_prompt(author=author_a)
        before = prompt.live_author
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        self.assertEqual(refetch(prompt).live_author, before)

    def test_author_account_deletion_does_not_affect_the_snapshot(self):
        author = User.objects.create_user("snap-stable-deleted", password="pw", first_name="Gone")
        prompt = self._published_prompt(author=author)
        before = prompt.live_author
        author.delete()
        reloaded = refetch(prompt)
        self.assertIsNone(reloaded.author_id)
        self.assertEqual(reloaded.live_author, before)

    def test_no_prompt_save_happens_from_a_bare_user_update(self):
        author = User.objects.create_user("snap-no-save-on-rename", password="pw")
        prompt = self._published_prompt(author=author)
        before_updated_at = prompt.updated_at
        User.objects.filter(pk=author.pk).update(first_name="Anything")
        self.assertEqual(refetch(prompt).updated_at, before_updated_at)


class NoAuthorPublishTests(TestCase):
    def test_publish_without_an_author_produces_the_explicit_empty_snapshot(self):
        actor = User.objects.create_user("snap-noauthor-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=None), actor=actor)
        self.assertEqual(
            prompt.live_author, {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": ""}
        )

    def test_assigning_an_author_without_republishing_does_not_change_the_snapshot(self):
        actor = User.objects.create_user("snap-noauthor-later-actor", password="pw")
        new_author = User.objects.create_user("snap-noauthor-later-new", password="pw", first_name="Late")
        prompt = full_cycle_to_published(make_prompt(author=None), actor=actor)
        before = prompt.live_author
        Prompt.objects.filter(pk=prompt.pk).update(author=new_author)
        self.assertEqual(refetch(prompt).live_author, before)

    def test_republish_with_an_author_replaces_the_empty_snapshot(self):
        actor = User.objects.create_user("snap-noauthor-republish-actor", password="pw")
        new_author = User.objects.create_user(
            "snap-noauthor-republish-new", password="pw", first_name="Now", last_name="Here"
        )
        prompt = full_cycle_to_published(make_prompt(author=None), actor=actor)
        Prompt.objects.filter(pk=prompt.pk).update(author=new_author)

        prompt = refetch(prompt)
        prompt.move_to_review(by=actor)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=actor)
        prompt.save()
        prompt = refetch(prompt)
        republished = publish_directly(prompt, by=actor)

        self.assertEqual(
            republished.live_author,
            {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Now Here"},
        )


class RepublishTests(TestCase):
    """A conscious republish (a real move_to_review -> approve -> publish
    cycle again) must replace the snapshot wholesale - never merge, never
    keep the stale value."""

    def test_republish_after_a_name_change_replaces_the_snapshot(self):
        author = User.objects.create_user("snap-republish-name", password="pw", first_name="Old")
        prompt = full_cycle_to_published(make_prompt(author=author), actor=author)
        old_snapshot = prompt.live_author
        self.assertEqual(old_snapshot["display_name"], "Old")

        User.objects.filter(pk=author.pk).update(first_name="New")

        prompt = refetch(prompt)
        prompt.move_to_review(by=author)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=author)
        prompt.save()
        prompt = refetch(prompt)
        republished = publish_directly(prompt, by=author)

        self.assertEqual(republished.live_author["display_name"], "New")
        self.assertNotEqual(republished.live_author, old_snapshot)

    def test_republish_after_an_author_change_replaces_the_snapshot(self):
        author_a = User.objects.create_user("snap-republish-author-a", password="pw", first_name="A")
        author_b = User.objects.create_user("snap-republish-author-b", password="pw", first_name="B")
        prompt = full_cycle_to_published(make_prompt(author=author_a), actor=author_a)
        old_snapshot = prompt.live_author

        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)

        prompt = refetch(prompt)
        prompt.move_to_review(by=author_b)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=author_b)
        prompt.save()
        prompt = refetch(prompt)
        republished = publish_directly(prompt, by=author_b)

        self.assertEqual(republished.live_author["display_name"], "B")
        self.assertNotEqual(republished.live_author, old_snapshot)

    def test_exactly_one_snapshot_survives_a_republish_no_merge(self):
        author = User.objects.create_user("snap-republish-nomerge", password="pw", first_name="First")
        prompt = full_cycle_to_published(make_prompt(author=author), actor=author)
        User.objects.filter(pk=author.pk).update(first_name="Second")

        prompt = refetch(prompt)
        prompt.move_to_review(by=author)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=author)
        prompt.save()
        prompt = refetch(prompt)
        republished = publish_directly(prompt, by=author)

        self.assertEqual(set(republished.live_author), {"schema", "display_name"})
        self.assertEqual(republished.live_author["display_name"], "Second")


class AdminPublishPathTests(TestCase):
    """The real ``core.admin.EditorialWorkflowAdminMixin.action_publish``
    path - the only production path that wraps the publish in an active
    ``reversion.create_revision()`` context."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(
            "snap-admin-editor", password="pw", is_staff=True, first_name="Ed", last_name="Itor"
        )
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user(
            "snap-admin-author", password="pw", is_staff=True, first_name="Ada", last_name="Lovelace"
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)

    def _approved_prompt(self):
        prompt = make_prompt(author=self.author)
        prompt.move_to_review(by=self.author)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=self.editor)
        prompt.save()
        return refetch(prompt)

    def test_admin_publish_action_creates_the_snapshot(self):
        prompt = self._approved_prompt()
        self.client.post(
            CHANGELIST_URL,
            data={"action": "action_publish", "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(
            reloaded.live_author,
            {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Ada Lovelace"},
        )

    def test_admin_publish_revision_root_version_carries_the_snapshot(self):
        import json

        prompt = self._approved_prompt()
        revisions_before = set(Revision.objects.values_list("id", flat=True))
        self.client.post(
            CHANGELIST_URL,
            data={"action": "action_publish", "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        new_revisions = Revision.objects.exclude(id__in=revisions_before).order_by("id")
        self.assertEqual(len(new_revisions), 1, "exactly one publish revision")
        revision = new_revisions[0]

        root_versions = [
            v for v in revision.version_set.select_related("content_type")
            if v.content_type.app_label == "prompts"
            and v.content_type.model == "prompt"
            and v.object_id == str(prompt.pk)
        ]
        self.assertEqual(len(root_versions), 1)
        fields = json.loads(root_versions[0].serialized_data)[0]["fields"]
        self.assertEqual(
            fields["live_author"],
            {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Ada Lovelace"},
        )

    def test_admin_publish_does_not_add_an_extra_save(self):
        """Beta 11.11C4E adds no new save call of its own. The existing
        publish action already performs exactly three, none of which C4E
        introduced: (1) ``EditorialWorkflowMixin._update_live_snapshot()``'s
        own ``save(update_fields=["live_i18n"])``, called from inside
        ``publish()`` itself before ``on_after_publish()`` ever runs; (2) the
        action's own full post-transition ``obj.save()``, which is what
        actually persists ``live_author`` (set in-memory by
        ``on_after_publish()`` after step 1's save already happened); (3) the
        action's partial ``last_published_revision_id`` save. Proven by
        spying on the real bound method rather than asserting a bare
        constant, so a future change to this count is caught either way."""
        prompt = self._approved_prompt()
        with mock.patch.object(Prompt, "save", autospec=True, side_effect=Prompt.save) as spy:
            self.client.post(
                CHANGELIST_URL,
                data={"action": "action_publish", "_selected_action": [str(prompt.pk)], "index": "0"},
                follow=True,
            )
        self.assertEqual(spy.call_count, 3)

    def test_admin_publish_creates_no_extra_revision_or_version_for_the_snapshot(self):
        prompt = self._approved_prompt()
        revisions_before = Revision.objects.count()
        self.client.post(
            CHANGELIST_URL,
            data={"action": "action_publish", "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        self.assertEqual(Revision.objects.count(), revisions_before + 1)


class EditorialViewPublishPathTests(TestCase):
    """The generic ``content/views/editorial.py`` transition dispatch -
    Prompt's own "review"/"approved" branches were rerouted onto C2A/C3A in
    Beta 11.11C4B, but "published" was deliberately left on the shared
    generic FSM + ``obj.save()`` path, and this must produce the identical
    snapshot contract with no duplicated snapshot logic in the view."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user("snap-view-editor", password="pw")
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user(
            "snap-view-author", password="pw", first_name="Grace", last_name="Hopper"
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def _approved_prompt(self):
        prompt = make_prompt(author=self.author)
        prompt.move_to_review(by=self.author)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=self.editor)
        prompt.save()
        return refetch(prompt)

    def test_review_update_publish_transition_creates_the_snapshot(self):
        prompt = self._approved_prompt()
        self.client.force_login(self.editor)
        resp = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "prompt", "object_id": prompt.pk, "status": "published"},
            follow=True,
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(
            reloaded.live_author,
            {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Grace Hopper"},
        )
        self.assertContains(resp, "Status updated.")

    def test_view_module_never_imports_or_defines_its_own_snapshot_logic(self):
        import pathlib

        import content.views.editorial as editorial_views_module

        source = pathlib.Path(editorial_views_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("live_author", source)
        self.assertNotIn("_build_live_author_snapshot", source)
        self.assertNotIn("PROMPT_AUTHOR_SNAPSHOT_SCHEMA", source)


class NoReviewPayloadEffectTests(TestCase):
    """Dauerhafte Nichtregression: Beta 11.11C4D's review payload v2 contract
    is completely untouched by this slice."""

    def test_live_author_is_not_part_of_the_review_payload(self):
        author = User.objects.create_user("snap-review-noeffect", password="pw", first_name="X")
        prompt = full_cycle_to_published(make_prompt(author=author), actor=author)
        payload = build_prompt_review_payload(refetch(prompt))
        self.assertNotIn("live_author", payload)
        self.assertEqual(set(payload["relations"]), {"author", "tools", "tags"})

    def test_changing_live_author_alone_does_not_change_the_review_fingerprint(self):
        author = User.objects.create_user("snap-review-fp-noeffect", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=author), actor=author)
        before = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        Prompt.objects.filter(pk=prompt.pk).update(
            live_author={"schema": "prompt-author-v1", "display_name": "Anything Else"}
        )
        after = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        self.assertEqual(before, after)

    def test_prompts_review_payload_module_is_unchanged_by_this_slice(self):
        import prompts.review_payload as review_payload_module

        source = open(review_payload_module.__file__, encoding="utf-8").read()
        self.assertNotIn("live_author", source)


class OtherEditorialTypesUnchangedTests(TestCase):
    """Guide/UseCase/Comparison get no new field and no snapshot logic in
    this Prompt-only slice."""

    def test_other_editorial_types_have_no_live_author_field(self):
        for model in (Guide, UseCase, Comparison):
            with self.subTest(model=model.__name__):
                self.assertFalse(hasattr(model, "live_author"))
                field_names = {f.name for f in model._meta.get_fields()}
                self.assertNotIn("live_author", field_names)

    def test_other_editorial_types_have_no_snapshot_builder(self):
        for model in (Guide, UseCase, Comparison):
            with self.subTest(model=model.__name__):
                self.assertFalse(hasattr(model, "_build_live_author_snapshot"))

    def test_core_editorial_base_class_unaffected(self):
        import core.models.editorial as editorial_module

        source = open(editorial_module.__file__, encoding="utf-8").read()
        self.assertNotIn("live_author", source)
        self.assertNotIn("PROMPT_AUTHOR_SNAPSHOT_SCHEMA", source)
