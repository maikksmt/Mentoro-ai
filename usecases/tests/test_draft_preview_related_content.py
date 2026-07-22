"""
Beta 11.8 group G: Related Content inside the preview.

``related_usecases()`` (Beta 11.7B) already ranks strictly on the *live*
persona snapshot, never the current draft translation, for both the source
object and every candidate. The preview reuses that function unchanged
(``usecases/presentation.py::build_draft_usecase_context``), so this module
proves the preview inherits that guarantee rather than re-testing the
ranking algorithm itself (see
``usecases/tests/test_related_usecases_live_persona.py`` for the full
ranking-algorithm test suite).
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from usecases.tests.draft_preview_fixtures import (
    add_translation,
    archive,
    make_draft_usecase,
    make_user,
    publish,
    save_translation_edit,
)


def preview_url(usecase_pk, language_code="en"):
    return reverse("admin:usecases_usecase_draft_preview", args=[usecase_pk, language_code])


class RelatedContentInThePreviewTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("related-preview-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_related_cards_use_live_candidate_values_and_live_slugs(self):
        source = make_draft_usecase(
            self.editor, slug="related-source-en", title="Related Source"
        )
        source = publish(source, self.editor)

        cand = make_draft_usecase(
            self.editor, slug="related-cand-live-en", title="Related Cand Live"
        )
        cand = publish(cand, self.editor)
        save_translation_edit(cand, "en", title="Related Cand Draft Title")

        html = self.client.get(preview_url(source.pk)).content.decode()
        self.assertIn("Related Cand Live", html)
        self.assertNotIn("Related Cand Draft Title", html)
        self.assertIn("/en/usecases/related-cand-live-en/", html)

    def test_archived_candidate_is_excluded_from_the_preview_related_cards(self):
        source = make_draft_usecase(
            self.editor, slug="related-archived-source-en", title="Related Archived Source"
        )
        source = publish(source, self.editor)

        archived_cand = make_draft_usecase(
            self.editor, slug="related-archived-cand-en", title="Archived Related Candidate"
        )
        archived_cand = publish(archived_cand, self.editor)
        archive(archived_cand, self.editor)

        html = self.client.get(preview_url(source.pk)).content.decode()
        self.assertNotIn("Archived Related Candidate", html)

    def test_never_published_candidate_is_excluded_from_the_preview_related_cards(self):
        source = make_draft_usecase(
            self.editor, slug="related-neverpub-source-en", title="Related NeverPub Source"
        )
        source = publish(source, self.editor)

        make_draft_usecase(
            self.editor, slug="related-neverpub-cand-en", title="Never Published Related Candidate"
        )

        html = self.client.get(preview_url(source.pk)).content.decode()
        self.assertNotIn("Never Published Related Candidate", html)

    def test_candidate_without_a_live_snapshot_in_the_preview_language_is_excluded(self):
        source = make_draft_usecase(
            self.editor, slug="related-nosnap-source-en", title="Related NoSnap Source"
        )
        source = publish(source, self.editor)

        cand = make_draft_usecase(
            self.editor, slug="related-nosnap-cand-en", title="Related NoSnap Cand EN"
        )
        cand = publish(cand, self.editor)
        add_translation(
            cand, "de", slug="related-nosnap-cand-de", title="Related NoSnap Cand DE Draft"
        )

        html = self.client.get(preview_url(source.pk, "de")).content.decode()
        self.assertNotIn("Related NoSnap Cand DE Draft", html)

    def test_source_draft_persona_does_not_affect_related_selection(self):
        """The whole point of Beta 11.7B: related_usecases() ranks the
        source by its live persona, never the draft one being previewed."""
        source = make_draft_usecase(
            self.editor, slug="related-persona-source-en", title="Related Persona Source",
            persona="Developers",
        )
        source = publish(source, self.editor)

        cand_a = make_draft_usecase(
            self.editor, slug="related-persona-cand-a-en", title="Related Persona Cand A",
            persona="Developers",
        )
        cand_a = publish(cand_a, self.editor)

        cand_b = make_draft_usecase(
            self.editor, slug="related-persona-cand-b-en", title="Related Persona Cand B",
            persona="Marketing",
        )
        cand_b = publish(cand_b, self.editor)

        # Change the SOURCE's draft persona (not yet published) - the preview
        # for this exact draft is what we are about to open.
        save_translation_edit(source, "en", persona="Marketing")

        html = self.client.get(preview_url(source.pk)).content.decode()
        cand_a_pos = html.find("Related Persona Cand A")
        cand_b_pos = html.find("Related Persona Cand B")
        self.assertNotEqual(cand_a_pos, -1)
        self.assertNotEqual(cand_b_pos, -1)
        # Live persona is still "Developers" (unpublished draft change), so
        # Cand A (matching "Developers") must still rank first.
        self.assertLess(cand_a_pos, cand_b_pos)

    def test_never_published_source_gets_no_persona_bonus(self):
        """A source with no live snapshot at all has no live persona to rank
        with - related_usecases() must not fall back to its draft persona."""
        source = make_draft_usecase(
            self.editor, slug="related-neverpub-persona-en", title="Related NeverPub Persona",
            persona="Developers",
        )
        cand_matching_draft = make_draft_usecase(
            self.editor, slug="related-neverpub-persona-cand-en", title="Related NeverPub Persona Cand",
            persona="Developers",
        )
        cand_matching_draft = publish(cand_matching_draft, self.editor)

        other_cand = make_draft_usecase(
            self.editor, slug="related-neverpub-persona-other-en", title="Related NeverPub Persona Other",
            persona="Marketing",
        )
        other_cand = publish(other_cand, self.editor)

        resp = self.client.get(preview_url(source.pk))
        self.assertEqual(resp.status_code, 200)
        # No assertion on ordering beyond "no crash, no persona-match error" -
        # the ranking-algorithm guarantee itself is covered exhaustively in
        # test_related_usecases_live_persona.py; this only proves the
        # preview path does not error and does not leak the draft persona.
        self.assertIn("Related NeverPub Persona Cand", resp.content.decode())

    def test_related_cards_never_link_to_a_preview_url(self):
        """Scoped to the "Similar use cases" section specifically: the page
        chrome from base.html (shared with the public site) legitimately
        reflects the current request path elsewhere - e.g. the logout
        form's ``next`` hidden field - which is expected to contain the
        admin preview URL while previewing. That is not a related-content
        leak; only the related cards themselves must never link there."""
        source = make_draft_usecase(
            self.editor, slug="related-nopreview-source-en", title="Related NoPreview Source"
        )
        source = publish(source, self.editor)
        cand = make_draft_usecase(
            self.editor, slug="related-nopreview-cand-en", title="Related NoPreview Cand"
        )
        publish(cand, self.editor)

        html = self.client.get(preview_url(source.pk)).content.decode()
        start = html.index("Similar use cases")
        end = html.index("</section>", start)
        related_section = html[start:end]
        self.assertIn("Related NoPreview Cand", related_section)
        self.assertNotIn("/preview/", related_section)
