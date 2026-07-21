"""
Coverage-Schritt 2: behavioral tests for content/views/seo_check.py - the
internal, staff-only SEO smoke-test tool at /ops/seo-check/.

Decorator order: `@login_required` wraps `@user_passes_test(lambda u: u.is_staff)`
(login_required is the outer/last-applied decorator here, the opposite order
from content/views/editorial.py). login_required runs first and redirects
anonymous users to the login page; for an authenticated-but-non-staff user
login_required passes them through and user_passes_test then performs its own
redirect-to-login - so both anonymous and non-staff authenticated users end up
with the same 302-to-login response, never a 403.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class SeoCheckAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="seo-staff", password="pass", is_staff=True)
        cls.regular = User.objects.create_user(username="seo-regular", password="pass", is_staff=False)
        cls.superuser = User.objects.create_superuser(
            username="seo-super", password="pass", email="super@example.com",
        )

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(reverse("ops_seo_check"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.headers["Location"])

    def test_authenticated_non_staff_is_redirected_to_login_not_403(self):
        self.client.login(username="seo-regular", password="pass")
        resp = self.client.get(reverse("ops_seo_check"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.headers["Location"])

    def test_staff_user_can_access(self):
        self.client.login(username="seo-staff", password="pass")
        resp = self.client.get(reverse("ops_seo_check"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "ops/seo_check.html")

    def test_superuser_can_access(self):
        self.client.login(username="seo-super", password="pass")
        resp = self.client.get(reverse("ops_seo_check"))
        self.assertEqual(resp.status_code, 200)


class SeoCheckGetContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="get-staff", password="pass", is_staff=True)

    def setUp(self):
        self.client.login(username="get-staff", password="pass")

    def test_get_shows_default_paths_and_no_results(self):
        resp = self.client.get(reverse("ops_seo_check"))
        self.assertEqual(resp.context["results"], [])
        self.assertIn("/de/glossary/", resp.context["default_paths"])
        self.assertEqual(resp.context["raw_paths"], resp.context["default_paths"])


class SeoCheckDiagnosticStateTests(TestCase):
    """Exercises run_checks() against real, controlled internal fixtures
    instead of mocking the view under test: /health/ is a bare
    HttpResponse("OK") with no <title>/meta description/canonical at all,
    giving a real "everything missing" diagnostic without any invalid-input
    trickery; an unknown path gives a real non-200 status branch."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="diag-staff", password="pass", is_staff=True)

    def setUp(self):
        self.client.login(username="diag-staff", password="pass")

    def _post(self, paths_text):
        return self.client.post(reverse("ops_seo_check"), {"paths": paths_text})

    def test_empty_paths_on_post_yields_no_results(self):
        resp = self._post("   ")
        self.assertEqual(resp.context["results"], [])

    def test_page_missing_all_seo_tags_reports_errors_and_warnings(self):
        resp = self._post("/health/")
        results = resp.context["results"]
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.status_code, 200)
        fields = {issue.field for issue in result.issues}
        self.assertIn("title", fields)
        self.assertIn("description", fields)
        self.assertIn("canonical", fields)
        self.assertIn("og:image", fields)
        levels = {issue.field: issue.level for issue in result.issues}
        self.assertEqual(levels["title"], "error")
        self.assertEqual(levels["description"], "error")
        self.assertEqual(levels["canonical"], "error")

    def test_unknown_path_reports_non_200_status_and_stops_extraction(self):
        resp = self._post("/this-route-does-not-exist-xyz/")
        result = resp.context["results"][0]
        self.assertEqual(result.status_code, 404)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].field, "status")
        self.assertEqual(result.issues[0].level, "error")
        self.assertEqual(result.extracted, {"title": "", "description": "", "canonical": "", "og_image": "", "hreflang": []})

    def test_absolute_url_input_is_normalized_to_its_path(self):
        resp = self._post("https://example.com/health/")
        result = resp.context["results"][0]
        self.assertEqual(result.path, "/health/")

    def test_multiple_paths_are_each_checked_in_order(self):
        resp = self._post("/health/\n/this-route-does-not-exist-xyz/")
        results = resp.context["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].path, "/health/")
        self.assertEqual(results[1].path, "/this-route-does-not-exist-xyz/")

    def test_a_real_published_page_has_no_technical_traceback_leak(self):
        resp = self._post("/en/")
        self.assertNotIn("Traceback", resp.content.decode())


class SeoCheckWarningHeuristicsTests(TestCase):
    """run_checks() drives its own internal django.test.Client HTTP round-
    trip to fetch each checked page - a real external-HTTP-style boundary,
    legitimate to mock per the slice's own mocking rules. Patching it here
    (not the seo_check_view under test) lets these tests pin the warning-
    level SEO heuristics (short/long title, long description, noindex
    robots, relative vs. foreign-domain canonical, relative og:image,
    hreflang missing de/en) deterministically, without depending on the
    incidental real content of unrelated site pages."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="warn-staff", password="pass", is_staff=True)

    def setUp(self):
        self.client.login(username="warn-staff", password="pass")

    def _fake_get(self, html: str):
        return HttpResponse(html.encode("utf-8"), status=200)

    def test_relative_canonical_relative_og_image_and_non_de_en_hreflang_warn(self):
        html = (
            "<html><head>"
            "<title>A Perfectly Normal Length Title</title>"
            '<meta name="description" content="'
            + ("Word " * 40).strip()
            + '">'
            '<meta name="robots" content="noindex,nofollow">'
            '<link rel="canonical" href="/relative-canonical-page/">'
            '<meta property="og:image" content="/relative-og-image.png">'
            '<link rel="alternate" hreflang="fr" href="https://example.com/fr/page/">'
            "</head><body>Body</body></html>"
        )
        with patch("content.views.seo_check.Client") as mock_client_cls:
            mock_client_cls.return_value.get.return_value = self._fake_get(html)
            resp = self.client.post(reverse("ops_seo_check"), {"paths": "/whatever/"})
        result = resp.context["results"][0]
        levels = {issue.field: issue.level for issue in result.issues}
        self.assertNotIn("title", levels)  # normal length: no title warning
        self.assertEqual(levels["description"], "warn")
        self.assertEqual(levels["robots"], "warn")
        self.assertEqual(levels["canonical"], "warn")
        self.assertEqual(levels["og:image"], "warn")
        self.assertEqual(levels["hreflang"], "warn")

    def test_too_short_title_warns(self):
        html = (
            "<html><head><title>Hi</title>"
            '<meta name="description" content="Short and fine.">'
            "</head><body>Body</body></html>"
        )
        with patch("content.views.seo_check.Client") as mock_client_cls:
            mock_client_cls.return_value.get.return_value = self._fake_get(html)
            resp = self.client.post(reverse("ops_seo_check"), {"paths": "/whatever/"})
        result = resp.context["results"][0]
        title_issues = [i for i in result.issues if i.field == "title"]
        self.assertEqual(len(title_issues), 1)
        self.assertEqual(title_issues[0].level, "warn")

    def test_absolute_canonical_matching_own_domain_does_not_warn_as_foreign(self):
        html = (
            "<html><head>"
            "<title>A Perfectly Normal Length Title</title>"
            '<meta name="description" content="Short and fine.">'
            '<link rel="canonical" href="http://testserver/canonical-target/">'
            "</head><body>Body</body></html>"
        )
        with patch("content.views.seo_check.Client") as mock_client_cls:
            mock_client_cls.return_value.get.return_value = self._fake_get(html)
            resp = self.client.post(reverse("ops_seo_check"), {"paths": "/whatever/"})
        result = resp.context["results"][0]
        fields_with_canonical_issue = [i for i in result.issues if i.field == "canonical"]
        self.assertEqual(fields_with_canonical_issue, [])
        self.assertEqual(result.extracted["canonical"], "http://testserver/canonical-target/")


class SeoCheckDefensiveExceptionPathTests(TestCase):
    """run_checks() is a clearly separated helper the view calls per path;
    mocking it (not the view under test) to simulate a single failing check
    exercises the view's own except-branch and its fail-safe formatting
    without leaking raw tracebacks to the rendered page."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="exc-staff", password="pass", is_staff=True)

    def setUp(self):
        self.client.login(username="exc-staff", password="pass")

    def test_a_failing_check_is_reported_as_an_error_without_a_traceback(self):
        with patch("content.views.seo_check.run_checks", side_effect=ValueError("boom")):
            resp = self.client.post(reverse("ops_seo_check"), {"paths": "/en/"})
        result = resp.context["results"][0]
        self.assertEqual(result.status_code, 0)
        self.assertEqual(result.issues[0].level, "error")
        self.assertEqual(result.issues[0].field, "exception")
        self.assertIn("boom", result.issues[0].msg)
        html = resp.content.decode()
        self.assertNotIn("Traceback", html)
        self.assertNotIn("ValueError", html)
