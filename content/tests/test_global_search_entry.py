"""
Beta 10.9: the navbar's search control.

Replaces the Beta 8.1 placeholder-dialog tests. The control used to be a
button that opened a "coming soon" dialog; it is now a plain link to the
search page, and these tests pin the properties that make it a *navigation*
item: it exists once on both surfaces, it is labelled, it marks itself active
on the search page, and it does not drag the logo into the active state with
it.
"""
import re

from django.conf import settings
from django.test import TestCase
from django.utils import translation


def logo_anchor(html: str) -> str:
    match = re.search(r"<a[^>]*>\s*MentoroAI\s*</a>", html, re.DOTALL)
    assert match is not None, "MentoroAI logo not found"
    return match.group(0)


def search_anchor(html: str) -> str:
    match = re.search(r'<a[^>]*id="global-search-link"[^>]*>', html, re.DOTALL)
    assert match is not None, "search link not found"
    return match.group(0)


class NavigationTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def page(self, path="/en/"):
        response = self.client.get(path, follow=True)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class EntryPointTests(NavigationTestCase):
    def test_search_control_is_a_link_not_a_dialog_trigger(self):
        html = self.page()
        anchor = search_anchor(html)
        self.assertIn('href="/en/search/"', anchor)
        self.assertNotIn("aria-controls", anchor)
        self.assertNotIn("aria-haspopup", anchor)
        self.assertNotRegex(html, r'<button[^>]*id="global-search-link"')

    def test_search_control_keeps_an_accessible_label(self):
        # The control shows only an icon, so the label is the only name a
        # screen reader has to work with.
        self.assertIn("aria-label=", search_anchor(self.page()))

    def test_one_control_serves_desktop_and_mobile(self):
        html = self.page()
        self.assertEqual(html.count('id="global-search-link"'), 1)
        # navbar-end carries no responsive prefix, so the single control is
        # visible at every width; the mobile dropdown needs no copy of it.
        navbar_end = html.split('class="navbar-end"', 1)[1]
        self.assertIn('id="global-search-link"', navbar_end)
        mobile_menu = html.split('id="mobile-nav-menu"', 1)[1].split("</ul>", 1)[0]
        self.assertNotIn("/en/search/", mobile_menu)

    def test_the_control_is_reachable_and_returns_the_search_page(self):
        response = self.client.get("/en/search/")
        self.assertEqual(response.status_code, 200)


class ActiveStateTests(NavigationTestCase):
    def test_search_link_is_active_on_the_search_page(self):
        anchor = search_anchor(self.page("/en/search/"))
        self.assertIn("btn-active", anchor)
        self.assertIn('aria-current="page"', anchor)

    def test_search_link_is_not_active_elsewhere(self):
        for path in ("/en/", "/en/guides/", "/en/catalog/"):
            with self.subTest(path=path):
                anchor = search_anchor(self.page(path))
                self.assertNotIn("btn-active", anchor)
                self.assertNotIn('aria-current="page"', anchor)

    def test_search_page_does_not_activate_another_section(self):
        html = self.page("/en/search/")
        for section in ("/en/guides/", "/en/catalog/", "/en/prompts/"):
            anchor = re.search(
                rf'<a[^>]*href="{re.escape(section)}"[^>]*>', html, re.DOTALL
            )
            with self.subTest(section=section):
                self.assertIsNotNone(anchor)
                self.assertNotIn("btn-active", anchor.group(0))


class LogoStaysNeutralTests(NavigationTestCase):
    """
    The logo is a home link, not a nav item.

    It marks the homepage with aria-current but never takes btn-active - that
    styling belongs to the section buttons. Activating search must not change
    that, on the search page least of all.
    """

    def test_logo_never_takes_the_active_button_style(self):
        for path in ("/en/", "/en/search/", "/en/guides/", "/de/search/"):
            with self.subTest(path=path):
                self.assertNotIn("btn-active", logo_anchor(self.page(path)))

    def test_logo_still_marks_the_homepage_for_assistive_technology(self):
        self.assertIn('aria-current="page"', logo_anchor(self.page("/en/")))

    def test_logo_is_not_current_on_the_search_page(self):
        self.assertNotIn('aria-current="page"', logo_anchor(self.page("/en/search/")))


class RoadmapDialogUntouchedTests(NavigationTestCase):
    def test_features_roadmap_dialog_still_present(self):
        # Removing the search dialog's JS must not have taken the roadmap
        # dialog's focus-restore block with it.
        self.assertIn('id="featuresmodal"', self.page("/en/"))
