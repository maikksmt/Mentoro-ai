"""
Beta 8.9: the desktop navbar must stay visible while scrolling via
position: sticky (not fixed), CSS-only, with no scroll JS. Mobile
navigation, the skip-link, and the search/roadmap dialogs must be
unaffected.
"""
from django.test import TestCase
from django.urls import reverse
from django.utils import translation


def _home():
    with translation.override("en"):
        return reverse("content:home")


class NavbarStickyStructureTests(TestCase):
    def setUp(self):
        resp = self.client.get(_home())
        self.assertEqual(resp.status_code, 200)
        self.html = resp.content.decode()

    def test_navbar_container_present(self):
        self.assertIn('class="navbar bg-base-100 shadow-sm z-100', self.html)

    def test_sticky_class_only_at_real_desktop_breakpoint(self):
        # lg: is the project's actual desktop-nav breakpoint (navbar-center
        # uses "hidden lg:flex", mobile trigger uses "lg:hidden").
        self.assertIn("lg:sticky", self.html)
        self.assertIn("lg:top-0", self.html)
        self.assertNotIn("sticky ", self.html.split("lg:sticky")[0][-40:])

    def test_no_fixed_class_introduced_on_navbar(self):
        navbar_start = self.html.index('class="navbar bg-base-100')
        navbar_tag_end = self.html.index(">", navbar_start)
        navbar_class_attr = self.html[navbar_start:navbar_tag_end]
        self.assertNotIn("fixed", navbar_class_attr)

    def test_existing_z_index_preserved_not_extreme(self):
        navbar_start = self.html.index('class="navbar bg-base-100')
        navbar_tag_end = self.html.index(">", navbar_start)
        navbar_class_attr = self.html[navbar_start:navbar_tag_end]
        self.assertIn("z-100", navbar_class_attr)
        self.assertNotIn("z-[9999]", navbar_class_attr)
        self.assertNotIn("z-9999", navbar_class_attr)

    def test_no_scroll_listener_script_added(self):
        self.assertNotIn("addEventListener('scroll'", self.html)
        self.assertNotIn('addEventListener("scroll"', self.html)
        self.assertNotIn("onscroll", self.html)

    def test_no_extra_spacer_element_after_navbar(self):
        # position: sticky stays in normal flow - there must be no
        # artificial spacer div inserted between the navbar and admin-nav
        # or main content to compensate for a (nonexistent) fixed navbar.
        between = self.html[self.html.index('class="navbar bg-base-100'):self.html.index('id="main-content"')]
        self.assertNotIn("spacer", between.lower())
        self.assertNotIn("navbar-spacer", between)

    def test_desktop_nav_links_all_present(self):
        for section in ("Glossary", "Guides", "Prompts", "Use cases", "Tools", "Comparisons"):
            self.assertIn(section, self.html)

    def test_home_logo_never_has_btn_active(self):
        logo_start = self.html.index("MentoroAI</a>")
        logo_snippet = self.html[max(0, logo_start - 250):logo_start]
        self.assertNotIn("btn-active", logo_snippet)

    def test_mobile_dropdown_structure_intact(self):
        self.assertIn('id="mobile-nav-trigger"', self.html)
        self.assertIn('aria-controls="mobile-nav-menu"', self.html)
        self.assertIn('id="mobile-nav-menu"', self.html)
        self.assertIn('aria-expanded="false"', self.html)

    def test_main_content_target_exists_exactly_once(self):
        self.assertEqual(self.html.count('id="main-content"'), 1)


class MainContentScrollOffsetTests(TestCase):
    """Beta 8.9: scroll-mt on #main-content must only apply once the
    sticky navbar is active (lg:+), matching its real rendered height per
    breakpoint - never an unconditional/mobile offset."""

    def setUp(self):
        resp = self.client.get(_home())
        self.html = resp.content.decode()
        main_start = self.html.index('id="main-content"')
        main_tag_end = self.html.index(">", main_start)
        self.main_tag = self.html[main_start:main_tag_end]

    def test_scroll_margin_only_applied_at_lg_and_xl_breakpoints(self):
        self.assertIn("lg:scroll-mt-", self.main_tag)

    def test_no_unconditional_scroll_margin_class(self):
        import re
        # Every scroll-mt-* utility on <main> must be breakpoint-prefixed.
        for match in re.finditer(r'(?:^|\s)(scroll-mt-\S+)', self.main_tag):
            self.fail(f"Unprefixed scroll-mt utility found: {match.group(1)}")


class SkipLinkRegressionTests(TestCase):
    """Beta 8.5 regression: skip-link must remain first focusable, jump to
    #main-content, and stay above the sticky navbar's z-layer."""

    def setUp(self):
        resp = self.client.get(_home())
        self.html = resp.content.decode()

    def test_skip_link_is_first_focusable_element_in_body(self):
        body_start = self.html.index("<body")
        first_a_pos = self.html.index("<a ", body_start)
        first_a_snippet = self.html[first_a_pos:first_a_pos + 200]
        self.assertIn('href="#main-content"', first_a_snippet)

    def test_skip_link_href_unchanged(self):
        self.assertIn('href="#main-content"', self.html)

    def test_skip_link_z_index_above_navbar(self):
        skip_start = self.html.index('href="#main-content"')
        skip_tag_end = self.html.index(">", skip_start)
        skip_snippet = self.html[skip_start:skip_tag_end]
        self.assertIn("focus:z-[9999]", skip_snippet)

    def test_no_positive_tabindex_introduced(self):
        import re
        for match in re.finditer(r'tabindex="(-?\d+)"', self.html):
            self.assertLessEqual(int(match.group(1)), 0, "positive tabindex found")


class DialogRegressionTests(TestCase):
    """The roadmap ("features") dialog must keep its ID and structure - a
    native <dialog> always renders above a position: sticky navbar regardless
    of its z-index, but the ID/JS hookup itself must be untouched by this
    slice. The search control stopped being a dialog in Beta 10.9 and is
    covered by content/tests/test_global_search_entry.py."""

    def setUp(self):
        resp = self.client.get(_home())
        self.html = resp.content.decode()

    def test_search_control_is_a_link_above_the_sticky_navbar(self):
        self.assertIn('id="global-search-link"', self.html)
        self.assertNotIn("searchmodal", self.html)

    def test_roadmap_features_dialog_present_with_unchanged_id(self):
        self.assertIn('id="featuresmodal"', self.html)

    def test_no_dialog_related_inline_scroll_js(self):
        self.assertNotIn("scrollIntoView", self.html)
