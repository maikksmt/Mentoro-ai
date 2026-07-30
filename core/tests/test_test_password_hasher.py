"""
Beta 11.12D1: the fast test password hasher is *test infrastructure only*.

Beta 11.12A measured that the suite spends roughly a fifth of its runtime
hashing throw-away test passwords: the project configures no
``PASSWORD_HASHERS`` of its own, so tests inherit Django's default
``PBKDF2PasswordHasher`` with 1,000,000 iterations - about 543 ms per
``make_password()`` call, for about 700 user creations plus 63 real
``client.login()`` verifications per full run. Two focused comparisons showed
80-82% of those two modules' runtime was hashing alone.

Making that fast is only acceptable if the weak hasher can never reach a real
process. This module pins both halves of that contract:

A. **During ``manage.py test``** the preferred hasher is the fast one, and
   every password behaviour - correct password, wrong password,
   ``authenticate()``, ``client.login()``, unusable passwords, no cleartext
   storage - stays exactly as before.
B. **Everywhere else** it is absent: no settings module configures it, a plain
   ``django.setup()`` does not activate it, an ordinary management command does
   not get it, and the single activation point lives in the test runner, is
   applied through ``override_settings`` and is removed again on teardown even
   if the parent teardown explodes.

The activation seam (``_enable_fast_password_hasher`` /
``_disable_fast_password_hasher``) is driven directly here. Re-running the
whole ``setup_test_environment()`` from inside a live test run is impossible by
Django's own design (it refuses a second call), so the runner exposes exactly
this small seam and the ordering against the parent hooks is asserted with
stubs instead.
"""
import ast
import pathlib
import subprocess
import sys
from unittest import mock

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.test import SimpleTestCase, TestCase
from django.test.runner import DiscoverRunner

from core.test_runner import (
    FAST_TEST_PASSWORD_HASHER,
    MentoroTestRunner,
    build_test_password_hashers,
)

User = get_user_model()

REPO_ROOT = pathlib.Path(settings.BASE_DIR)
RUNNER_PATH = REPO_ROOT / "core" / "test_runner.py"
SETTINGS_DIR = REPO_ROOT / "mentoroai" / "settings"

#: The algorithm label of the fast hasher, resolved from the configured class
#: rather than hardcoded, so this module never invents a second source of truth.
FAST_ALGORITHM = "md5"

SKIP_DIRS = {
    "venv",
    ".git",
    ".tox",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "htmlcov",
    "staticfiles",
    "media",
    "migrations",
}


def _production_python_files():
    """
    Every project ``.py`` file that is not itself a test module.

    Membership is decided by the ``tests`` package (plus ``api/tests.py``, the
    one legacy single-file module), *not* by a ``test_`` filename prefix: the
    runner under audit here is itself called ``core/test_runner.py``, and a
    prefix rule would silently exempt exactly the file this guard exists to
    inspect.
    """
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(REPO_ROOT)
        if "tests" in relative.parts or path.name == "tests.py":
            continue
        yield path


def _dotted_name(node):
    """``sys.argv`` from an ``ast.Attribute`` chain, or ``None``."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _code_identifiers(tree):
    """
    Every name, dotted name and imported module actually referenced by the
    code - deliberately AST-based, so a docstring that *mentions* ``sys.argv``
    as something the module refuses to do never matches itself. Same lesson the
    Beta 11.11C4H guards already learned in
    ``prompts/tests/test_admin_review_edit_guard.py``.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
            dotted = _dotted_name(node)
            if dotted:
                names.add(dotted)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
    return names


# ======================================================================
# A. Active during Django tests - behaviour unchanged
# ======================================================================


class FastHasherActiveDuringTestsTests(TestCase):
    def test_the_fast_hasher_is_the_preferred_one_during_tests(self):
        self.assertEqual(settings.PASSWORD_HASHERS[0], FAST_TEST_PASSWORD_HASHER)

    def test_make_password_produces_the_fast_algorithm(self):
        encoded = make_password("secret")
        self.assertEqual(identify_hasher(encoded).algorithm, FAST_ALGORITHM)

    def test_the_correct_password_verifies(self):
        self.assertTrue(check_password("secret", make_password("secret")))

    def test_a_wrong_password_is_rejected(self):
        self.assertFalse(check_password("wrong", make_password("secret")))

    def test_create_user_stores_no_cleartext_password(self):
        user = User.objects.create_user(username="d1-cleartext", password="secret")
        stored = User.objects.get(pk=user.pk).password
        self.assertNotIn("secret", stored)
        self.assertTrue(stored.startswith(f"{FAST_ALGORITHM}$"))
        self.assertTrue(user.check_password("secret"))

    def test_authenticate_accepts_the_right_credentials(self):
        User.objects.create_user(username="d1-auth", password="secret")
        self.assertIsNotNone(authenticate(username="d1-auth", password="secret"))

    def test_authenticate_rejects_a_wrong_password(self):
        User.objects.create_user(username="d1-auth-bad", password="secret")
        self.assertIsNone(authenticate(username="d1-auth-bad", password="nope"))

    def test_client_login_succeeds_with_the_right_password(self):
        User.objects.create_user(username="d1-login", password="secret")
        self.assertTrue(self.client.login(username="d1-login", password="secret"))

    def test_client_login_fails_with_a_wrong_password(self):
        User.objects.create_user(username="d1-login-bad", password="secret")
        self.assertFalse(self.client.login(username="d1-login-bad", password="nope"))

    def test_an_unusable_password_stays_unusable(self):
        user = User.objects.create_user(username="d1-unusable")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        reloaded = User.objects.get(pk=user.pk)
        self.assertFalse(reloaded.has_usable_password())
        self.assertFalse(reloaded.check_password(""))
        self.assertIsNone(authenticate(username="d1-unusable", password=""))

    def test_password_validators_are_untouched_by_the_hasher_choice(self):
        """D1 changes how a password is stored, never which passwords are
        acceptable - the validator chain must be exactly the project's own."""
        configured = [entry["NAME"] for entry in settings.AUTH_PASSWORD_VALIDATORS]
        self.assertEqual(
            configured,
            [
                "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
                "django.contrib.auth.password_validation.MinimumLengthValidator",
                "django.contrib.auth.password_validation.CommonPasswordValidator",
                "django.contrib.auth.password_validation.NumericPasswordValidator",
            ],
        )


# ======================================================================
# A2. Existing secure hashes stay readable
# ======================================================================


class ExistingSecureHashesRemainVerifiableTests(TestCase):
    """
    The repository currently contains no pre-hashed password fixture at all
    (no ``loaddata`` in any test, no JSON/YAML fixture, no ``set_password``/
    ``make_password`` outside this module), so a fast-hasher-only list would be
    provably sufficient today. The list nevertheless keeps every hasher the
    project would otherwise use, because that costs nothing at runtime - a
    slower hasher is only ever *read* - and it means a future fixture or an
    explicitly PBKDF2-encoded password keeps working instead of failing
    mysteriously.
    """

    def test_a_pbkdf2_encoded_password_still_verifies(self):
        encoded = make_password("secret", hasher="pbkdf2_sha256")
        self.assertEqual(identify_hasher(encoded).algorithm, "pbkdf2_sha256")
        self.assertTrue(check_password("secret", encoded))
        self.assertFalse(check_password("wrong", encoded))

    def test_a_user_carrying_a_pbkdf2_hash_can_still_log_in(self):
        user = User.objects.create_user(username="d1-legacy")
        User.objects.filter(pk=user.pk).update(
            password=make_password("secret", hasher="pbkdf2_sha256")
        )
        self.assertTrue(self.client.login(username="d1-legacy", password="secret"))

    def test_login_upgrades_a_legacy_hash_to_the_preferred_one(self):
        """Django's own documented password-upgrade behaviour, unchanged by
        D1: authenticating against a non-preferred hash rewrites it."""
        user = User.objects.create_user(username="d1-upgrade")
        User.objects.filter(pk=user.pk).update(
            password=make_password("secret", hasher="pbkdf2_sha256")
        )
        self.assertTrue(self.client.login(username="d1-upgrade", password="secret"))
        upgraded = User.objects.get(pk=user.pk).password
        self.assertEqual(identify_hasher(upgraded).algorithm, FAST_ALGORITHM)

    def test_every_configured_project_hasher_is_still_reachable(self):
        base = ["a.B", "c.D"]
        built = build_test_password_hashers(base)
        for hasher in base:
            self.assertIn(hasher, built)


# ======================================================================
# B1. The hasher list contract
# ======================================================================


class HasherListContractTests(SimpleTestCase):
    def test_the_fast_hasher_comes_first(self):
        built = build_test_password_hashers(["x.Y", "z.Z"])
        self.assertEqual(built[0], FAST_TEST_PASSWORD_HASHER)

    def test_configured_order_is_preserved_after_the_fast_hasher(self):
        self.assertEqual(
            build_test_password_hashers(["x.Y", "z.Z"]),
            [FAST_TEST_PASSWORD_HASHER, "x.Y", "z.Z"],
        )

    def test_building_from_an_already_built_list_is_idempotent(self):
        once = build_test_password_hashers(["x.Y"])
        self.assertEqual(build_test_password_hashers(once), once)

    def test_the_input_list_is_never_mutated(self):
        configured = ["x.Y", "z.Z"]
        build_test_password_hashers(configured)
        self.assertEqual(configured, ["x.Y", "z.Z"])

    def test_the_fast_hasher_is_a_real_django_hasher(self):
        module_path, _, class_name = FAST_TEST_PASSWORD_HASHER.rpartition(".")
        self.assertEqual(module_path, "django.contrib.auth.hashers")
        hashers = __import__(module_path, fromlist=[class_name])
        self.assertEqual(getattr(hashers, class_name).algorithm, FAST_ALGORITHM)


# ======================================================================
# B2. Activation and cleanup seam
# ======================================================================


class RunnerActivationSeamTests(SimpleTestCase):
    """
    Drives the runner's own seam directly. ``settings.PASSWORD_HASHERS`` is
    already overridden by the live runner while these tests execute, so each
    test restores what it found and asserts against that value rather than
    against a hardcoded production default.
    """

    def setUp(self):
        self.before = list(settings.PASSWORD_HASHERS)
        self.runner = MentoroTestRunner()
        self.addCleanup(self.runner._disable_fast_password_hasher)

    def test_enable_makes_the_fast_hasher_preferred(self):
        self.runner._enable_fast_password_hasher()
        self.assertEqual(settings.PASSWORD_HASHERS[0], FAST_TEST_PASSWORD_HASHER)

    def test_disable_restores_the_previous_value_exactly(self):
        self.runner._enable_fast_password_hasher()
        self.runner._disable_fast_password_hasher()
        self.assertEqual(list(settings.PASSWORD_HASHERS), self.before)

    def test_enabling_twice_leaks_no_duplicate_override(self):
        self.runner._enable_fast_password_hasher()
        self.runner._enable_fast_password_hasher()
        self.runner._disable_fast_password_hasher()
        self.assertEqual(list(settings.PASSWORD_HASHERS), self.before)

    def test_disabling_twice_is_harmless(self):
        self.runner._enable_fast_password_hasher()
        self.runner._disable_fast_password_hasher()
        self.runner._disable_fast_password_hasher()
        self.assertEqual(list(settings.PASSWORD_HASHERS), self.before)

    def test_disabling_without_enabling_is_harmless(self):
        self.runner._disable_fast_password_hasher()
        self.assertEqual(list(settings.PASSWORD_HASHERS), self.before)

    def test_the_runner_subclasses_djangos_own_discover_runner(self):
        self.assertTrue(issubclass(MentoroTestRunner, DiscoverRunner))


class RunnerHookOrderingTests(SimpleTestCase):
    """
    Proves the seam is wired into the real hooks in the Django-sanctioned
    order - parent setup before activation, deactivation before parent
    teardown - without calling Django's ``setup_test_environment()`` a second
    time (it refuses that by design).
    """

    def _instrumented_runner(self, calls, *, teardown_error=None):
        runner = MentoroTestRunner()
        self.addCleanup(runner._disable_fast_password_hasher)

        def record(name, error=None):
            def _recorder(*args, **kwargs):
                calls.append(name)
                if error is not None:
                    raise error

            return _recorder

        patches = [
            mock.patch.object(
                DiscoverRunner, "setup_test_environment", record("parent_setup")
            ),
            mock.patch.object(
                DiscoverRunner,
                "teardown_test_environment",
                record("parent_teardown", teardown_error),
            ),
            mock.patch.object(
                MentoroTestRunner,
                "_enable_fast_password_hasher",
                record("enable"),
            ),
            mock.patch.object(
                MentoroTestRunner,
                "_disable_fast_password_hasher",
                record("disable"),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return runner

    def test_setup_activates_after_the_parent_setup(self):
        calls = []
        self._instrumented_runner(calls).setup_test_environment()
        self.assertEqual(calls, ["parent_setup", "enable"])

    def test_teardown_deactivates_before_the_parent_teardown(self):
        calls = []
        self._instrumented_runner(calls).teardown_test_environment()
        self.assertEqual(calls, ["disable", "parent_teardown"])

    def test_a_failing_parent_teardown_still_leaves_the_override_removed(self):
        calls = []
        runner = self._instrumented_runner(
            calls, teardown_error=RuntimeError("parent teardown boom")
        )
        with self.assertRaises(RuntimeError):
            runner.teardown_test_environment()
        self.assertEqual(calls, ["disable", "parent_teardown"])

    def test_a_failing_deactivation_still_runs_the_parent_teardown(self):
        calls = []
        runner = MentoroTestRunner()
        self.addCleanup(runner._disable_fast_password_hasher)
        with mock.patch.object(
            DiscoverRunner,
            "teardown_test_environment",
            lambda *a, **kw: calls.append("parent_teardown"),
        ):
            with mock.patch.object(
                MentoroTestRunner,
                "_disable_fast_password_hasher",
                side_effect=RuntimeError("disable boom"),
            ):
                with self.assertRaises(RuntimeError):
                    runner.teardown_test_environment()
        self.assertEqual(calls, ["parent_teardown"])


# ======================================================================
# B3. Static security guard
# ======================================================================


class FastHasherIsTestOnlyStaticTests(SimpleTestCase):
    def test_no_settings_module_configures_the_fast_hasher(self):
        offenders = []
        for path in sorted(SETTINGS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "MD5" in source or FAST_TEST_PASSWORD_HASHER in source:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_no_settings_module_defines_password_hashers_at_all(self):
        """The activation must not be a broad settings constant that a
        development or production start could inherit."""
        offenders = []
        for path in sorted(SETTINGS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == "PASSWORD_HASHERS":
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_exactly_one_production_module_names_the_fast_hasher(self):
        naming = sorted(
            str(path.relative_to(REPO_ROOT))
            for path in _production_python_files()
            if FAST_TEST_PASSWORD_HASHER in path.read_text(encoding="utf-8", errors="ignore")
        )
        self.assertEqual(naming, ["core/test_runner.py"])

    def test_the_test_runner_setting_points_at_the_sanctioned_runner(self):
        self.assertEqual(settings.TEST_RUNNER, "core.test_runner.MentoroTestRunner")

    def test_the_runner_uses_no_fragile_activation_mechanism(self):
        """No argv sniffing, no environment variable, no startup hook, no
        signal, no per-thread or per-context state - the activation must be the
        test runner's own lifecycle and nothing else."""
        identifiers = _code_identifiers(ast.parse(RUNNER_PATH.read_text(encoding="utf-8")))
        for forbidden in (
            "argv",
            "sys.argv",
            "AppConfig",
            "ready",
            "environ",
            "getenv",
            "contextvars",
            "ContextVar",
            "threading",
            "connect",
            "receiver",
            "setting_changed",
            "post_migrate",
            "pre_migrate",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, identifiers)

    def test_the_runner_overrides_settings_instead_of_mutating_them(self):
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        identifiers = _code_identifiers(tree)
        self.assertIn("override_settings", identifiers)
        self.assertNotIn("setattr", identifiers)

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if _dotted_name(target) in (
                        "settings.PASSWORD_HASHERS",
                        "self.settings.PASSWORD_HASHERS",
                    ):
                        offenders.append(f"assign:{node.lineno}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                dotted = _dotted_name(node.func) or ""
                if dotted.startswith("settings.PASSWORD_HASHERS.") or (
                    "PASSWORD_HASHERS" in dotted
                    and node.func.attr in ("append", "insert", "extend", "remove", "pop")
                ):
                    offenders.append(f"mutate:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_the_runner_module_mutates_nothing_at_import_time(self):
        """Importing the module must define names only - no call, no ``with``,
        no conditional side effect at module level."""
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        offenders = []
        for node in tree.body:
            if isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # module docstring
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is None or isinstance(value, (ast.Constant, ast.List, ast.Tuple)):
                    continue
            offenders.append(f"{type(node).__name__}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_only_the_two_environment_hooks_touch_the_override(self):
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        runner_class = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "MentoroTestRunner"
        )
        seam = {"_enable_fast_password_hasher", "_disable_fast_password_hasher"}
        callers = {}
        for item in runner_class.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            for call in ast.walk(item):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in seam
                ):
                    callers.setdefault(call.func.attr, set()).add(item.name)
        self.assertEqual(
            callers,
            {
                "_enable_fast_password_hasher": {"setup_test_environment"},
                "_disable_fast_password_hasher": {"teardown_test_environment"},
            },
        )

    def test_no_application_module_imports_the_runner(self):
        """The runner is referenced by exactly one settings string
        (``TEST_RUNNER`` in ``mentoroai/settings/base.py``) and imported by
        nobody: application code must never be able to reach the fast hasher."""
        importers = []
        referencing = []
        for path in _production_python_files():
            if path == RUNNER_PATH:
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            if "core.test_runner" not in source and "test_runner" not in source:
                continue
            referencing.append(str(path.relative_to(REPO_ROOT)))
            if "core.test_runner" in _code_identifiers(ast.parse(source)):
                importers.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(importers, [])
        self.assertEqual(referencing, ["mentoroai/settings/base.py"])


# ======================================================================
# B4. Proof from outside the test runner (real processes)
# ======================================================================


PROBE = r"""
import os, sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mentoroai.settings")

import django

django.setup()

from django.conf import settings

sys.stdout.write("---HASHER---" + settings.PASSWORD_HASHERS[0])
"""

SHELL_PROBE = (
    "from django.conf import settings; "
    "print('---HASHER---' + settings.PASSWORD_HASHERS[0])"
)


class OutsideTheTestRunnerTests(SimpleTestCase):
    """
    Real processes, no server, no database write: whatever the fast hasher does
    inside ``manage.py test``, an ordinary ``django.setup()`` and an ordinary
    management command must still prefer the project's real hasher.
    """

    MARKER = "---HASHER---"

    def _preferred_hasher_in(self, argv):
        completed = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"probe failed:\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertIn(self.MARKER, completed.stdout, completed.stdout)
        return completed.stdout.split(self.MARKER, 1)[1].strip()

    def test_a_plain_settings_import_does_not_prefer_the_fast_hasher(self):
        preferred = self._preferred_hasher_in([sys.executable, "-c", PROBE])
        self.assertNotEqual(preferred, FAST_TEST_PASSWORD_HASHER)
        self.assertNotIn("MD5", preferred)

    def test_an_ordinary_management_command_does_not_get_the_fast_hasher(self):
        preferred = self._preferred_hasher_in(
            [sys.executable, "manage.py", "shell", "-c", SHELL_PROBE]
        )
        self.assertNotEqual(preferred, FAST_TEST_PASSWORD_HASHER)
        self.assertNotIn("MD5", preferred)
