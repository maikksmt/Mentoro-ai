from urllib.parse import urlparse, parse_qs

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.forms import UserAccountForm

User = get_user_model()


class AccountDashboardAuthTests(TestCase):
    """AccountDashboardView delegates auth entirely to LoginRequiredMixin."""

    def test_anonymous_user_is_redirected_to_login_with_next(self):
        dashboard_path = reverse("account_dashboard")
        resp = self.client.get(dashboard_path)

        self.assertEqual(resp.status_code, 302)
        parsed = urlparse(resp.url)
        self.assertIn("login", parsed.path)
        self.assertEqual(parse_qs(parsed.query).get("next"), [dashboard_path])

    def test_anonymous_post_is_also_redirected_before_touching_form(self):
        resp = self.client.post(reverse("account_dashboard"), {"first_name": "X"})
        self.assertEqual(resp.status_code, 302)


class AccountDashboardGetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="jane",
            email="jane@example.com",
            password="pass1234",
            first_name="Jane",
            last_name="Doe",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_authenticated_get_renders_dashboard(self):
        resp = self.client.get(reverse("account_dashboard"))

        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "account/dashboard.html")

    def test_context_contains_bound_user_account_form(self):
        resp = self.client.get(reverse("account_dashboard"))

        form = resp.context["user_account_form"]
        self.assertIsInstance(form, UserAccountForm)
        self.assertEqual(form.instance, self.user)
        self.assertEqual(form.instance.first_name, "Jane")

    def test_context_seo_reflects_request(self):
        resp = self.client.get(reverse("account_dashboard"))

        seo = resp.context["seo"]
        self.assertEqual(seo.title, "My account")
        self.assertTrue(seo.canonical.endswith(reverse("account_dashboard")))
        self.assertEqual(seo.json_ld["@type"], "ProfilePage")


class AccountDashboardPostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="john",
            email="john@example.com",
            password="pass1234",
            first_name="John",
            last_name="Smith",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_valid_post_updates_profile_and_redirects_with_message(self):
        resp = self.client.post(
            reverse("account_dashboard"),
            {"first_name": "Johnny", "last_name": "Smithers"},
        )

        self.assertRedirects(resp, reverse("account_dashboard"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Johnny")
        self.assertEqual(self.user.last_name, "Smithers")

        messages = list(get_messages(resp.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertEqual(str(messages[0]), "Your profile has been updated.")

    def test_invalid_post_rerenders_with_errors_and_does_not_save(self):
        resp = self.client.post(
            reverse("account_dashboard"),
            {"first_name": "", "last_name": ""},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "account/dashboard.html")

        form = resp.context["user_account_form"]
        self.assertFalse(form.is_valid())
        self.assertIn("first_name", form.errors)
        self.assertIn("last_name", form.errors)

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "John")
        self.assertEqual(self.user.last_name, "Smith")

    def test_invalid_post_produces_no_success_message(self):
        resp = self.client.post(
            reverse("account_dashboard"),
            {"first_name": "", "last_name": ""},
        )
        messages = list(get_messages(resp.wsgi_request))
        self.assertEqual(messages, [])
