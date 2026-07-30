"""
Beta 11.12D1: the project's Django test runner.

Its single job is to make throw-away test passwords cheap. Beta 11.12A
measured that the suite inherits Django's default
``PBKDF2PasswordHasher`` (1,000,000 iterations, ~543 ms per hash) for roughly
700 user creations and 63 real ``client.login()`` verifications per full run -
about a fifth of the total runtime spent on work no test asserts.

Why this lives in a test runner and not in a settings module. The settings
package switches on ``DJANGO_ENV``, and ``DJANGO_ENV=ci``/``test`` resolves to
``mentoroai.settings.development`` - the very same module a developer's
``runserver`` loads, and the same value CI exports for its ``migrate``,
``compilemessages`` and ``collectstatic`` steps. Any settings-level
configuration would therefore hand the weak hasher to real processes. A
``TEST_RUNNER``, by contrast, is instantiated by exactly one caller:
``manage.py test``. Nothing else in Django reads it.

The contract this module keeps:

* the weak hasher is applied in :meth:`setup_test_environment` only - never at
  import time, never from a signal, an ``AppConfig.ready()``, a thread-local, an
  environment variable or a ``sys.argv`` inspection;
* it is applied through Django's own ``override_settings``, so
  ``setting_changed`` receivers see it and the previous value is restored
  verbatim - the settings list itself is never mutated;
* it is removed in :meth:`teardown_test_environment` before the parent teardown
  runs, and that removal happens even when the parent teardown raises;
* the fast hasher is only *preferred*, not exclusive: every hasher the project
  would otherwise use stays in the list, so an existing PBKDF2/Argon2/BCrypt
  encoded password remains verifiable (and is upgraded by Django's ordinary
  password-upgrade path on first successful login).

Pinned by ``core/tests/test_test_password_hasher.py``, which also proves from
real subprocesses that a plain ``django.setup()`` and an ordinary management
command still prefer the project's real hasher.
"""
from django.conf import settings
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

#: Deliberately weak, deliberately fast, and deliberately named in exactly one
#: production module. MD5 is acceptable *only* because it never leaves the
#: test environment established by :class:`MentoroTestRunner`.
FAST_TEST_PASSWORD_HASHER = "django.contrib.auth.hashers.MD5PasswordHasher"


def build_test_password_hashers(configured):
    """
    The fast hasher first, then every configured hasher, each exactly once.

    Prepending rather than replacing keeps already-encoded passwords readable;
    the extra entries cost nothing, because a non-preferred hasher is only ever
    used to *verify* an existing hash. Idempotent, so an already-built list
    passed back in is returned unchanged, and the input is never mutated.
    """
    return [
        FAST_TEST_PASSWORD_HASHER,
        *(hasher for hasher in configured if hasher != FAST_TEST_PASSWORD_HASHER),
    ]


class MentoroTestRunner(DiscoverRunner):
    """
    Django's own runner plus one test-only password-hasher override.

    Everything else - discovery, database setup, parallelism, shuffling,
    ``--durations`` - is inherited unchanged.
    """

    #: Holds the active ``override_settings`` instance while the test
    #: environment is up; ``None`` means "not applied". Class-level default so
    #: no ``__init__`` override is needed.
    _password_hasher_override = None

    def _enable_fast_password_hasher(self):
        """Apply the override once; a second call is a no-op."""
        if self._password_hasher_override is not None:
            return
        override = override_settings(
            PASSWORD_HASHERS=build_test_password_hashers(settings.PASSWORD_HASHERS)
        )
        override.enable()
        self._password_hasher_override = override

    def _disable_fast_password_hasher(self):
        """Restore the previous value; safe to call twice or without enabling."""
        override = self._password_hasher_override
        self._password_hasher_override = None
        if override is not None:
            override.disable()

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._enable_fast_password_hasher()

    def teardown_test_environment(self, **kwargs):
        try:
            self._disable_fast_password_hasher()
        finally:
            super().teardown_test_environment(**kwargs)
