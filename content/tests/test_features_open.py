"""
Beta 11.15D: the "What's coming to MentoroAI" roadmap modal
(templates/partials/features_open.html, rendered once on the homepage) must
never claim a feature is still upcoming once it has actually shipped, and
must never leak internal audit terminology (finding codes, attack paths,
permission-implementation detail) into public-facing copy.
"""
import re

from django.test import Client, TestCase
from django.utils import translation

_SVG_BLOCK_RE = re.compile(r"<svg\b.*?</svg>", re.DOTALL)


def _home_html(lang="en"):
    with translation.override(lang):
        resp = Client().get(f"/{lang}/")
    assert resp.status_code == 200
    html = resp.content.decode()
    if lang != "en":
        translation.activate("en")
    return html


def _modal_html(lang="en"):
    """Just the <dialog id="featuresmodal">...</dialog> block, so content
    assertions can't accidentally match unrelated page markup (SVG path
    data, asset hashes, etc.) elsewhere on the homepage."""
    html = _home_html(lang)
    start = html.index('<dialog id="featuresmodal"')
    end = html.index("</dialog>", start)
    return html[start:end]


def _modal_text(lang="en"):
    """The modal block with inline decorative SVG icons stripped, so a
    substring check for a short code (e.g. "M1", "H1") can't accidentally
    match arbitrary SVG path-data tokens (e.g. `d="M15.75...`)."""
    return _SVG_BLOCK_RE.sub("", _modal_html(lang))


class RoadmapModalRendersTests(TestCase):
    def test_en_homepage_renders_the_roadmap_modal(self):
        html = _home_html("en")
        self.assertIn('<dialog id="featuresmodal"', html)

    def test_de_homepage_renders_the_roadmap_modal(self):
        html = _home_html("de")
        self.assertIn('<dialog id="featuresmodal"', html)


class OpenBeta12TopicsAppearTests(TestCase):
    """The still-open Beta 12 topics are visible in plain, non-technical
    English (and, through the existing i18n workflow, German)."""

    def test_granular_roles_topic_present(self):
        html = _modal_html("en")
        self.assertIn("editorial roles", html.lower())

    def test_ownership_aware_editing_topic_present(self):
        html = _modal_html("en")
        self.assertIn("ownership", html.lower())

    def test_secure_preview_history_recovery_topic_present(self):
        html = _modal_html("en")
        self.assertIn("revision history", html.lower())
        self.assertIn("recovery", html.lower())

    def test_editor_review_counter_topic_present(self):
        html = _modal_html("en")
        self.assertIn("waiting for review", html.lower())

    def test_author_review_result_topic_present(self):
        html = _modal_html("en")
        self.assertIn("my content", html.lower())
        self.assertIn("approved", html.lower())


class ImplementedFeaturesNoLongerListedAsOpenTests(TestCase):
    """Beta 11 already shipped tool-to-tool comparisons and the base search
    across content types; the modal must not present either as still
    missing."""

    def test_compare_view_no_longer_claimed_as_upcoming(self):
        html = _modal_html("en")
        self.assertNotIn("Compare view: side-by-side feature comparison", html)

    def test_search_is_not_claimed_to_not_exist_yet(self):
        html = _modal_html("en")
        self.assertNotIn("Unified search across Tools, Guides, Prompts, Use-Cases with filters and tags.", html)


class NoInternalAuditLeakageTests(TestCase):
    """Internal finding codes and attack-path language from the Beta 11.15A
    permission audit must never reach a public template."""

    def test_no_internal_finding_codes(self):
        html = _modal_text("en")
        for code in ("K1", "H1", "M1", "N1"):
            self.assertNotIn(code, html)

    def test_no_security_attack_language(self):
        html = _modal_text("en")
        lowered = html.lower()
        for term in ("idor", "tamper", "recovery of foreign", "exploit"):
            self.assertNotIn(term, lowered)

    def test_no_beta_codenames_leak_into_markup(self):
        html = _modal_text("en")
        self.assertNotIn("Beta 12", html)
        self.assertNotIn("11.15", html)


class ExistingModalStructureUnchangedTests(TestCase):
    """The audit only touches copy; the dialog shell, trigger button and
    close affordance must render exactly as before."""

    def test_trigger_button_and_dialog_still_connected(self):
        html = _home_html("en")
        self.assertIn('onclick="featuresmodal.showModal()"', html)
        self.assertIn('aria-controls="featuresmodal"', html)

    def test_github_discussions_link_still_present(self):
        html = _home_html("en")
        self.assertIn("https://github.com/maikksmt/mentoro-ai/discussions", html)
