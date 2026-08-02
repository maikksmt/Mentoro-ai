"""
Beta 11.1: verifies and locks in the fix for a stored-XSS path in the
editorial admin's readonly rendering.

Guide/Prompt/UseCase/Comparison all store author-authored rich text (via
TinyMCE) directly in translated fields, and their ModelAdmin classes define
intro()/body()/outro() display methods that Django's own admin machinery
substitutes for the real field whenever a change form renders that field
readonly - which happens either via an explicit readonly_fields entry, or
automatically for the whole form when has_view_permission() is True but
has_change_permission() is False for that object (Django's normal
view-only contract, see AdminReadonlyField.contents() in
django.contrib.admin.helpers).

Before this fix those display methods returned
``mark_safe(value or "")`` on the raw stored value - no sanitization at
all - so any HTML an author saved (via a TinyMCE session, a bulk import, or
a direct DB write) executed verbatim in the browser of any staff user who
merely opened the object's readonly change form, including staff who lack
edit rights on that object entirely.

Every relevant staff role is exercised through the real admin change-form
request (not by calling the ModelAdmin methods directly), because the
vulnerability only exists on that real rendering path.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

XSS_IMG = '<img src="x" onerror="alert(\'xss\')">'
XSS_SCRIPT = "<script>alert('xss')</script>"
SAFE_MARKER = "Totally-Visible-Marker-Text"

# TinyMCE's own upload-handler JS (see TINYMCE_DEFAULT_CONFIG in
# mentoroai/settings/base.py) legitimately contains the bare word "onerror"
# on any page that renders an *editable* TinyMCE widget anywhere (e.g. an
# unrelated inline formset's "add another" extra row, which stays editable
# independently of the main object's own readonly state). Asserting against
# this exact attribute+value instead of the bare word avoids that false
# positive while still catching the real payload verbatim.
XSS_PAYLOAD_MARKER = 'onerror="alert('


class ReadonlyRichtextXSSTestsBase(TestCase):
    """Shared setup: a viewer-only staff user (Author group, not the
    object's own author) who can open the change form but cannot edit it -
    exactly the readonly rendering path the vulnerability lived on."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

        self.author = User.objects.create_user(
            username="rt-author", password="pw", is_staff=True
        )
        self.author.groups.add(Group.objects.get(name="Author"))

        self.viewer = User.objects.create_user(
            username="rt-viewer", password="pw", is_staff=True
        )
        self.viewer.groups.add(Group.objects.get(name="Author"))

        self.editor = User.objects.create_user(
            username="rt-editor", password="pw", is_staff=True
        )
        self.editor.groups.add(Group.objects.get(name="Editor"))


class GuideReadonlyRichtextXSSTests(ReadonlyRichtextXSSTestsBase):
    def setUp(self):
        super().setUp()
        self.guide = Guide.objects.create(
            author=self.author, status=EditorialWorkflowMixin.STATUS_DRAFT
        )
        self.guide.create_translation(
            "en",
            title="Payload Guide",
            slug="payload-guide-xss",
            intro=f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
            body=f"{XSS_SCRIPT}<p>{SAFE_MARKER}</p>",
        )

    def _change_url(self):
        return reverse("admin:guides_guide_change", args=[self.guide.pk])

    def test_readonly_viewer_never_receives_onerror_or_script(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(XSS_PAYLOAD_MARKER, html)
        self.assertNotIn("<script>", html)
        self.assertIn(SAFE_MARKER, html)

    def test_stored_data_is_unchanged_by_rendering(self):
        self.client.force_login(self.viewer)
        self.client.get(self._change_url())
        refreshed = Guide.objects.get(pk=self.guide.pk)
        self.assertEqual(
            refreshed.safe_translation_getter("intro", language_code="en"),
            f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
        )

    def test_editor_with_change_rights_still_gets_editable_tinymce_widget(self):
        self.client.force_login(self.editor)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("intro", resp.context["adminform"].form.fields)
        widget = resp.context["adminform"].form.fields["intro"].widget
        self.assertEqual(widget.__class__.__name__, "TinyMCE")


class PromptReadonlyRichtextXSSTests(ReadonlyRichtextXSSTestsBase):
    def setUp(self):
        super().setUp()
        self.prompt = Prompt.objects.create(
            author=self.author, status=EditorialWorkflowMixin.STATUS_DRAFT
        )
        self.prompt.create_translation(
            "en",
            title="Payload Prompt",
            slug="payload-prompt-xss",
            intro=f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
            body=f"{XSS_SCRIPT}<p>{SAFE_MARKER}</p>",
            outro=f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
        )

    def _change_url(self):
        return reverse("admin:prompts_prompt_change", args=[self.prompt.pk])

    def test_readonly_viewer_never_receives_onerror_or_script(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(XSS_PAYLOAD_MARKER, html)
        self.assertNotIn("<script>", html)
        self.assertIn(SAFE_MARKER, html)

    def test_editor_with_change_rights_still_gets_editable_tinymce_widget(self):
        self.client.force_login(self.editor)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        widget = resp.context["adminform"].form.fields["body"].widget
        self.assertEqual(widget.__class__.__name__, "TinyMCE")


class UseCaseReadonlyRichtextXSSTests(ReadonlyRichtextXSSTestsBase):
    def setUp(self):
        super().setUp()
        self.usecase = UseCase.objects.create(
            author=self.author, status=EditorialWorkflowMixin.STATUS_DRAFT
        )
        self.usecase.create_translation(
            "en",
            title="Payload UseCase",
            slug="payload-usecase-xss",
            persona="Founder",
            intro=f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
            body=f"{XSS_SCRIPT}<p>{SAFE_MARKER}</p>",
            outro=f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
        )

    def _change_url(self):
        return reverse("admin:usecases_usecase_change", args=[self.usecase.pk])

    def test_readonly_viewer_never_receives_onerror_or_script(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(XSS_PAYLOAD_MARKER, html)
        self.assertNotIn("<script>", html)
        self.assertIn(SAFE_MARKER, html)

    def test_editor_with_change_rights_still_gets_editable_tinymce_widget(self):
        self.client.force_login(self.editor)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        widget = resp.context["adminform"].form.fields["intro"].widget
        self.assertEqual(widget.__class__.__name__, "TinyMCE")


class ComparisonReadonlyRichtextXSSTests(ReadonlyRichtextXSSTestsBase):
    def setUp(self):
        super().setUp()
        self.comparison = Comparison.objects.create(
            author=self.author, status=EditorialWorkflowMixin.STATUS_DRAFT
        )
        self.comparison.create_translation(
            "en",
            title="Payload Comparison",
            slug="payload-comparison-xss",
            intro=f"{XSS_IMG}<p>{SAFE_MARKER}</p>",
            body=f"{XSS_SCRIPT}<p>{SAFE_MARKER}</p>",
        )

    def _change_url(self):
        return reverse("admin:compare_comparison_change", args=[self.comparison.pk])

    def test_readonly_viewer_never_receives_onerror_or_script(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(XSS_PAYLOAD_MARKER, html)
        self.assertNotIn("<script>", html)
        self.assertIn(SAFE_MARKER, html)

    def test_editor_with_change_rights_still_gets_editable_tinymce_widget(self):
        self.client.force_login(self.editor)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        widget = resp.context["adminform"].form.fields["intro"].widget
        self.assertEqual(widget.__class__.__name__, "TinyMCE")
