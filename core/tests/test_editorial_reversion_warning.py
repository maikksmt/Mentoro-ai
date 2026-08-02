"""
Beta 11.11D3C: the rollback warning shown on the reversion revert form.

A rollback replaces the current editing state - including translations,
sections and entries added since - and restores the workflow/publication
status stored in the selected revision. That is deliberate behaviour, so the
form has to say so *before* the confirming save.

Scope of the notice, asserted below:

* it appears on ``admin:<app>_<model>_revision`` for all four editorial roots;
* it is a real warning semantically (``role="alert"``) and visually (the
  admin's own ``errornote`` styling - no new CSS, no JavaScript, no modal);
* it is translated (English source, German translation);
* it never appears on an ordinary changeform or on the history list;
* it never appears on the recover form, whose wording would be wrong there
  (nothing is being replaced when a deleted object is restored) - so
  ``recover_form_template`` is deliberately left at reversion's default.

Language handling note: the admin URLs live outside ``i18n_patterns``, so the
active language comes from ``LocaleMiddleware`` reading ``Accept-Language``.
Every German request below therefore restores the ambient language in
``addCleanup`` - a German request otherwise leaves German active for the rest
of the process and only fails under ``--shuffle``/``--reverse``.
"""
import itertools

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Revision, Version

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide
from mentoroai.tests.utils import silence_django_request_warnings
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

_counter = itertools.count()

#: The exact English source copy, as specified for D3C.
ENGLISH_WARNING_FRAGMENTS = (
    (
        "Warning: Restoring this revision replaces the current editing state with "
        "the selected version."
    ),
    "Changes made after this revision",
    "including translations, sections, and entries",
    "may be removed.",
    (
        "The workflow and publication status will also be restored to the state "
        "stored in this revision."
    ),
)

#: Distinctive fragments of the German translation.
GERMAN_WARNING_FRAGMENTS = (
    "Achtung: Beim Wiederherstellen dieser Revision",
    "Bearbeitungsstand durch die ausgew",
    "einschlie",
    "Veröffentlichungsstatus",
)


def _unique(prefix):
    return f"{prefix}-{next(_counter)}"


def refetch(model, pk):
    return model.objects.get(pk=pk)


class ReversionWarningTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            "d3c-warn-su", "d3c-warn-su@example.com", "pw"
        )
        cls.author = User.objects.create_user(
            "d3c-warn-author", password="pw", is_staff=True
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.other_author = User.objects.create_user(
            "d3c-warn-other", password="pw", is_staff=True
        )
        cls.other_author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        # Any German request below flips the process-wide active language;
        # restore whatever was active when this test started.
        self.addCleanup(translation.activate, translation.get_language() or "en")
        self.client.force_login(self.superuser)

    def latest_version_for(self, model, pk):
        meta = model._meta
        return Version.objects.get(
            revision=Revision.objects.latest("pk"),
            object_id=str(pk),
            content_type__app_label=meta.app_label,
            content_type__model=meta.model_name,
        )

    def submit(self, prefix, pk):
        resp = self.client.post(
            reverse(f"admin:{prefix}_changelist"),
            data={
                "action": "action_submit_for_review",
                "_selected_action": [str(pk)],
                "index": "0",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

    # -- object builders ------------------------------------------------

    def make_prompt(self, *, author=None):
        with translation.override("en"):
            obj = Prompt.objects.create(
                author=author or self.superuser, status=Workflow.STATUS_DRAFT
            )
            obj.create_translation(
                "en", title="P", intro="i", body="b", outro="o",
                slug=_unique("d3c-warn-prompt"),
            )
        return obj

    def make_guide(self, *, author=None):
        with translation.override("en"):
            obj = Guide.objects.create(
                author=author or self.superuser, status=Workflow.STATUS_DRAFT
            )
            obj.create_translation(
                "en", title="G", intro="i", body="b", slug=_unique("d3c-warn-guide")
            )
        return obj

    def make_usecase(self, *, author=None):
        with translation.override("en"):
            obj = UseCase.objects.create(
                author=author or self.superuser, status=Workflow.STATUS_DRAFT
            )
            obj.create_translation(
                "en", title="U", intro="i", body="b", outro="o", persona="p",
                slug=_unique("d3c-warn-case"),
            )
        return obj

    def make_comparison(self, *, author=None):
        with translation.override("en"):
            tool = Tool.objects.create(slug=_unique("d3c-warn-tool"))
            tool.create_translation("en", name="T")
            obj = Comparison.objects.create(
                author=author or self.superuser, status=Workflow.STATUS_DRAFT
            )
            obj.create_translation(
                "en", title="C", intro="i", body="b", slug=_unique("d3c-warn-cmp")
            )
        return obj

    def surfaces(self):
        """``(label, admin prefix, model, object with one real revision)``."""
        for label, prefix, model, factory in (
            ("prompt", "prompts_prompt", Prompt, self.make_prompt),
            ("guide", "guides_guide", Guide, self.make_guide),
            ("usecase", "usecases_usecase", UseCase, self.make_usecase),
            ("comparison", "compare_comparison", Comparison, self.make_comparison),
        ):
            obj = factory()
            self.submit(prefix, obj.pk)
            version = self.latest_version_for(model, obj.pk)
            yield label, prefix, model, obj, version

    def revision_url(self, prefix, pk, version_id):
        return reverse(f"admin:{prefix}_revision", args=[pk, version_id])


class RevisionFormWarningTests(ReversionWarningTestCase):
    def test_english_warning_is_rendered_on_every_revision_form(self):
        for label, prefix, _model, obj, version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    self.revision_url(prefix, obj.pk, version.pk),
                    headers={"accept-language": "en"},
                )
                self.assertEqual(resp.status_code, 200)
                for fragment in ENGLISH_WARNING_FRAGMENTS:
                    self.assertContains(resp, fragment, html=False)

    def test_warning_carries_accessible_alert_semantics(self):
        for label, prefix, _model, obj, version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    self.revision_url(prefix, obj.pk, version.pk),
                    headers={"accept-language": "en"},
                )
                self.assertContains(resp, 'role="alert"')

    def test_german_translation_is_rendered(self):
        for label, prefix, _model, obj, version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    self.revision_url(prefix, obj.pk, version.pk),
                    headers={"accept-language": "de"},
                )
                self.assertEqual(resp.status_code, 200)
                for fragment in GERMAN_WARNING_FRAGMENTS:
                    self.assertContains(resp, fragment, html=False)

    def test_reversions_own_instruction_text_is_kept(self):
        """The warning is added to reversion's block, not instead of it."""
        for label, prefix, _model, obj, version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    self.revision_url(prefix, obj.pk, version.pk),
                    headers={"accept-language": "en"},
                )
                self.assertContains(resp, "Press the save button below to revert")

    def test_the_revision_form_is_still_submittable(self):
        for label, prefix, _model, obj, version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    self.revision_url(prefix, obj.pk, version.pk),
                    headers={"accept-language": "en"},
                )
                self.assertContains(resp, "<form")
                # reversion renders its submit rows with ``is_popup=1``, so
                # the revert form offers "Save" only - never "Save and
                # continue editing".
                self.assertContains(resp, 'name="_save"')


class WarningIsScopedToTheRevisionFormTests(ReversionWarningTestCase):
    def test_ordinary_changeform_has_no_warning(self):
        for label, prefix, _model, obj, _version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    reverse(f"admin:{prefix}_change", args=[obj.pk]),
                    headers={"accept-language": "en"},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, ENGLISH_WARNING_FRAGMENTS[0])

    def test_history_list_has_no_warning(self):
        for label, prefix, _model, obj, _version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    reverse(f"admin:{prefix}_history", args=[obj.pk]),
                    headers={"accept-language": "en"},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, ENGLISH_WARNING_FRAGMENTS[0])

    def test_changelist_has_no_warning(self):
        for label, prefix, _model, obj, _version in self.surfaces():
            with self.subTest(surface=label):
                resp = self.client.get(
                    reverse(f"admin:{prefix}_changelist"),
                    headers={"accept-language": "en"},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, ENGLISH_WARNING_FRAGMENTS[0])

    def test_recover_form_is_left_at_reversions_default(self):
        """Recover restores a *deleted* object: nothing is replaced and no
        later work is removed, so the rollback wording would be wrong. D3C
        deliberately does not set ``recover_form_template``."""
        obj = self.make_prompt()
        self.submit("prompts_prompt", obj.pk)
        version = self.latest_version_for(Prompt, obj.pk)
        Prompt.objects.filter(pk=obj.pk).delete()

        resp = self.client.get(
            reverse("admin:prompts_prompt_recover", args=[version.pk]),
            headers={"accept-language": "en"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Press the save button below to recover")
        self.assertNotContains(resp, ENGLISH_WARNING_FRAGMENTS[0])


class RevisionFormPermissionsAreUnchangedTests(ReversionWarningTestCase):
    """The warning is presentation only - it must not widen who may roll back."""

    def test_author_can_open_and_confirm_a_rollback_of_their_own_prompt(self):
        obj = self.make_prompt(author=self.author)
        self.submit("prompts_prompt", obj.pk)
        version = self.latest_version_for(Prompt, obj.pk)

        self.client.force_login(self.author)
        resp = self.client.get(self.revision_url("prompts_prompt", obj.pk, version.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ENGLISH_WARNING_FRAGMENTS[0])

    def test_foreign_author_cannot_confirm_a_rollback(self):
        obj = self.make_prompt(author=self.author)
        self.submit("prompts_prompt", obj.pk)
        version = self.latest_version_for(Prompt, obj.pk)
        title_before = refetch(Prompt, obj.pk).safe_translation_getter(
            "title", language_code="en"
        )

        self.client.force_login(self.other_author)
        with silence_django_request_warnings():
            resp = self.client.post(
                self.revision_url("prompts_prompt", obj.pk, version.pk),
                data={
                    "author": str(self.author.pk),
                    "review_note": "",
                    "published_at_0": "",
                    "published_at_1": "",
                    "tools": [],
                    "slug": refetch(Prompt, obj.pk).safe_translation_getter(
                        "slug", language_code="en"
                    ),
                    "title": "Hijacked",
                    "intro": "i",
                    "body": "b",
                    "outro": "o",
                    "_continue": "Save",
                },
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            refetch(Prompt, obj.pk).safe_translation_getter("title", language_code="en"),
            title_before,
        )
