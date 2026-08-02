"""
Coverage-Schritt 3: behavioral tests for newsletter/views.py, closing the
gaps left by the existing focused test files (test_views.py,
test_views_public.py, test_unsubscribe.py) - rate limiting, honeypot,
mail-service failure handling, token-based confirm/unsubscribe idempotency,
and their security properties (no token leaks, no user-enumeration via
response status, generic error messaging).

System boundaries mocked here (per the slice's own mocking rules):
  - email sending (django.core.mail via mail.outbox, or send_double_opt_in_email
    patched at the view's import site for the "mail service unavailable" case)
  - wall-clock time, only at django.core.signing's own time.time() call site,
    to produce a genuinely expired TimestampSigner token
  - the rate-limit cache (a real system boundary - django.core.cache), cleared
    in setUp per project convention and pre-seeded only to reach the global
    rate limit deterministically without spamming 30+ real requests
"""
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.signing import TimestampSigner
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from newsletter.models import Subscriber
from newsletter.security import RATE_LIMITS, NewsletterEmailTemporarilyUnavailable

User = get_user_model()


class LanguageIsolationMixin:
    """Deterministic active-language setup/teardown (see Beta 10 Phase 7
    guidance): every test in this module builds its URLs via reverse(),
    which picks up whatever language happens to be ambient at call time - a
    prior test in another module (e.g. one hitting a raw "/de/..." path)
    can leave "de" active for the rest of the process otherwise."""

    def setUp(self):
        super().setUp()
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)


class SubscribeHappyPathTests(LanguageIsolationMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_email_is_normalized_case_and_whitespace(self):
        self.client.post(reverse("newsletter:subscribe"), {"email": "  USER@Example.COM  "})
        self.assertTrue(Subscriber.objects.filter(email="user@example.com").exists())

    def test_previously_unsubscribed_address_can_resubscribe(self):
        sub = Subscriber.objects.create(email="was-unsubscribed@example.com", double_opt_in=True)
        sub.mark_unsubscribed(reason="test")
        mail.outbox.clear()
        resp = self.client.post(
            reverse("newsletter:subscribe"), {"email": sub.email}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_unconfirmed_pending_subscriber_gets_another_confirmation_email(self):
        Subscriber.objects.create(email="pending@example.com", double_opt_in=False)
        mail.outbox.clear()
        self.client.post(reverse("newsletter:subscribe"), {"email": "pending@example.com"})
        self.assertEqual(len(mail.outbox), 1)

    def test_empty_email_shows_form_error_without_persisting(self):
        resp = self.client.post(reverse("newsletter:subscribe"), {"email": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["form"].is_valid())
        self.assertEqual(Subscriber.objects.count(), 0)

    def test_invalid_email_shows_form_error(self):
        resp = self.client.post(reverse("newsletter:subscribe"), {"email": "not-an-email"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("email", resp.context["form"].errors)


class SubscribeAbuseGuardTests(LanguageIsolationMixin, TestCase):
    """Honeypot and minimum-fill-time checks live in SubscriptionForm.clean();
    exercised here through the real view/form contract, not by calling the
    form in isolation."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_honeypot_field_filled_blocks_submission_silently(self):
        resp = self.client.post(
            reverse("newsletter:subscribe"),
            {"email": "bot@example.com", "company": "Acme Bots Inc"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Subscriber.objects.filter(email="bot@example.com").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_submission_faster_than_minimum_fill_time_blocks_silently(self):
        import time
        resp = self.client.post(
            reverse("newsletter:subscribe"),
            {"email": "toofast@example.com", "form_rendered_at": time.time()},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Subscriber.objects.filter(email="toofast@example.com").exists())
        self.assertEqual(len(mail.outbox), 0)


class SubscribeRateLimitTests(LanguageIsolationMixin, TestCase):
    """enforce_subscribe_limits() (newsletter/security.py) is called from
    SubscribeView.form_valid() before Subscriber.objects.get_or_create() -
    every rejection here must leave no Subscriber row and no outgoing mail,
    and must respond with the same generic message/redirect regardless of
    which limit tripped (no enumeration signal via status code)."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_ip_rate_limit_exceeded_creates_no_subscriber_and_still_redirects(self):
        for i in range(RATE_LIMITS.ip_max_requests):
            self.client.post(reverse("newsletter:subscribe"), {"email": f"ip-fill-{i}@example.com"})
        mail.outbox.clear()

        resp = self.client.post(reverse("newsletter:subscribe"), {"email": "ip-blocked@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Subscriber.objects.filter(email="ip-blocked@example.com").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_same_email_resubmitted_immediately_hits_cooldown_no_duplicate_mail(self):
        email = "cooldown@example.com"
        self.client.post(reverse("newsletter:subscribe"), {"email": email})
        self.assertEqual(len(mail.outbox), 1)

        resp = self.client.post(reverse("newsletter:subscribe"), {"email": email})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)  # no second mail sent
        self.assertEqual(Subscriber.objects.filter(email=email).count(), 1)

    def test_global_rate_limit_shows_service_unavailable_warning(self):
        # Pre-seed the global counter (django.core.cache is the real system
        # boundary the rate limiter relies on) so the limit trips
        # deterministically on the very next request, instead of spamming
        # 30+ real POSTs from the same test IP - which would trip the much
        # lower per-IP limit (5/10min) first and never reach this branch.
        cache.set("newsletter:global", RATE_LIMITS.global_max_requests, timeout=60)
        resp = self.client.post(
            reverse("newsletter:subscribe"),
            {"email": "global-limited@example.com"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Subscriber.objects.filter(email="global-limited@example.com").exists())
        self.assertEqual(len(mail.outbox), 0)
        html = resp.content.decode()
        self.assertIn("try again later", html.lower())


class SubscribeMailServiceFailureTests(LanguageIsolationMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_mail_service_failure_keeps_subscriber_but_shows_warning(self):
        with patch(
            "newsletter.views.send_double_opt_in_email",
            side_effect=NewsletterEmailTemporarilyUnavailable("smtp_send_failed"),
        ):
            resp = self.client.post(
                reverse("newsletter:subscribe"),
                {"email": "mailfail@example.com"},
                follow=True,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Subscriber.objects.filter(email="mailfail@example.com").exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("try again later", resp.content.decode().lower())


class ConfirmSubscriptionTests(LanguageIsolationMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_valid_token_confirms_sets_timestamp_and_clears_token(self):
        subscriber = Subscriber.objects.create(email="confirm-me@example.com")
        token = subscriber.refresh_doi_token()
        resp = self.client.get(reverse("newsletter:confirm", args=[token]))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "newsletter/confirm_success.html")
        subscriber = Subscriber.objects.get(pk=subscriber.pk)
        self.assertTrue(subscriber.double_opt_in)
        self.assertIsNotNone(subscriber.confirmed_at)
        self.assertIsNone(subscriber.doi_token)

    def test_unknown_token_returns_404_with_generic_message_no_leak(self):
        # Note: the token itself is the URL path segment for this view, so
        # it naturally reappears in the page's own canonical URL/nav "next"
        # field - that is not a technical leak. What matters is that the
        # error body stays the single generic message, with no traceback
        # and no distinguishing detail about *why* the token failed.
        resp = self.client.get(reverse("newsletter:confirm", args=["totally-bogus-token"]))
        self.assertEqual(resp.status_code, 404)
        self.assertTemplateUsed(resp, "newsletter/confirm_error.html")
        html = resp.content.decode()
        self.assertNotIn("Traceback", html)
        self.assertIn("invalid or has already been used", html.lower())

    def test_reusing_the_same_token_after_confirmation_is_invalid(self):
        subscriber = Subscriber.objects.create(email="reuse-token@example.com")
        token = subscriber.refresh_doi_token()
        self.client.get(reverse("newsletter:confirm", args=[token]))

        resp = self.client.get(reverse("newsletter:confirm", args=[token]))
        self.assertEqual(resp.status_code, 404)
        self.assertTemplateUsed(resp, "newsletter/confirm_error.html")


class UnsubscribeFormViewTests(LanguageIsolationMixin, TestCase):
    def test_get_displays_form(self):
        resp = self.client.get(reverse("newsletter:unsubscribe"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("form", resp.context)

    def test_valid_active_subscriber_gets_unsubscribed(self):
        sub = Subscriber.objects.create(email="active@example.com", double_opt_in=True)
        resp = self.client.post(
            reverse("newsletter:unsubscribe"), {"email": sub.email}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        sub = Subscriber.objects.get(pk=sub.pk)
        self.assertFalse(sub.double_opt_in)
        self.assertIsNotNone(sub.unsubscribed_at)

    def test_unknown_email_shows_generic_error_same_redirect_target(self):
        resp = self.client.post(
            reverse("newsletter:unsubscribe"), {"email": "never-subscribed@example.com"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("newsletter:unsubscribe_done"))

    def test_already_unsubscribed_is_idempotent_timestamp_unchanged(self):
        sub = Subscriber.objects.create(email="already-gone@example.com", double_opt_in=True)
        sub.mark_unsubscribed(reason="first")
        first_timestamp = sub.unsubscribed_at

        resp = self.client.post(
            reverse("newsletter:unsubscribe"), {"email": sub.email}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        sub = Subscriber.objects.get(pk=sub.pk)
        self.assertEqual(sub.unsubscribed_at, first_timestamp)

    def test_missing_email_shows_form_error(self):
        resp = self.client.post(reverse("newsletter:unsubscribe"), {"email": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["form"].is_valid())


class UnsubscribeConfirmTokenTests(LanguageIsolationMixin, TestCase):
    """The one-click unsubscribe link at /u/<token>/ (UnsubscribeConfirmView).
    Note (documented, not a security/permission/token-integrity defect - a
    cosmetic-only finding): the success context always sets "already": False
    - a second click on the same link finds no double_opt_in=True row left
    (mark_unsubscribed() already flipped it to False) and falls into the
    generic "invalid" branch instead of an "already unsubscribed" message.
    Tested here as the real, current contract - not invented as "already"
    ever being True."""

    def _sign(self, email):
        return TimestampSigner().sign_object({"email": email})

    def test_valid_token_unsubscribes_and_renders_success_context(self):
        sub = Subscriber.objects.create(email="oneclick@example.com", double_opt_in=True)
        token = self._sign(sub.email)
        resp = self.client.get(reverse("newsletter:unsubscribe_confirm", args=[token]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["already"], False)
        sub = Subscriber.objects.get(pk=sub.pk)
        self.assertFalse(sub.double_opt_in)
        self.assertIsNotNone(sub.unsubscribed_at)
        self.assertEqual(sub.unsubscribed_reason, "link")

    def test_clicking_the_same_link_twice_is_treated_as_invalid_not_already(self):
        sub = Subscriber.objects.create(email="oneclick-twice@example.com", double_opt_in=True)
        token = self._sign(sub.email)
        self.client.get(reverse("newsletter:unsubscribe_confirm", args=[token]))

        resp = self.client.get(reverse("newsletter:unsubscribe_confirm", args=[token]))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.context.get("invalid"))
        self.assertIsNone(resp.context.get("email"))

    def test_unknown_subscriber_email_in_token_is_invalid_no_leak(self):
        token = self._sign("never-existed@example.com")
        resp = self.client.get(reverse("newsletter:unsubscribe_confirm", args=[token]))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.context.get("invalid"))
        html = resp.content.decode()
        self.assertNotIn("never-existed@example.com", html)

    def test_tampered_token_is_rejected_as_invalid(self):
        sub = Subscriber.objects.create(email="tampered@example.com", double_opt_in=True)
        token = self._sign(sub.email)
        resp = self.client.get(reverse("newsletter:unsubscribe_confirm", args=[token + "x"]))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.context.get("invalid"))
        sub = Subscriber.objects.get(pk=sub.pk)
        self.assertTrue(sub.double_opt_in)  # unchanged - tampered token never acted

    def test_expired_token_is_rejected_as_invalid(self):
        sub = Subscriber.objects.create(email="expired@example.com", double_opt_in=True)
        far_past = timezone.now().timestamp() - (60 * 60 * 24 * 31)  # 31 days ago
        with patch("django.core.signing.time.time", return_value=far_past):
            token = self._sign(sub.email)
        resp = self.client.get(reverse("newsletter:unsubscribe_confirm", args=[token]))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.context.get("invalid"))
        sub = Subscriber.objects.get(pk=sub.pk)
        self.assertTrue(sub.double_opt_in)  # unchanged - expired token never acted

    def test_post_is_not_allowed(self):
        sub = Subscriber.objects.create(email="method@example.com", double_opt_in=True)
        token = self._sign(sub.email)
        resp = self.client.post(reverse("newsletter:unsubscribe_confirm", args=[token]))
        self.assertEqual(resp.status_code, 405)


class UnsubscribeDoneViewTests(LanguageIsolationMixin, TestCase):
    def test_renders(self):
        resp = self.client.get(reverse("newsletter:unsubscribe_done"))
        self.assertEqual(resp.status_code, 200)
