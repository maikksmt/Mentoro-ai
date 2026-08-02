"""
Beta 11.11B1: the editorial django-reversion registration is deterministic.

Beta 11.11A proved that the shipped registration was decided by a startup race
rather than by any written contract: ``MentoroAdminConfig`` (INSTALLED_APPS[0])
ran admin autodiscovery, ``VersionAdmin.__init__()`` auto-registered each root
model with a ``follow`` derived from its inlines, and the editorial apps' own
``reversion.register(..., follow=("translations",))`` calls then raised
``RegistrationError`` into an ``except: pass``.

These tests pin the replacement contract from
``core.reversion_registration``:

A. the central registration runs before admin autodiscovery, and no
   ``VersionAdmin`` ever auto-registers anything (subprocess, real startup);
B. the live registry matches :data:`EDITORIAL_REVERSION_MANIFEST` exactly -
   models, ``follow``, ``fields`` and every serialization option;
C. a pre-registration fails fast instead of being silently accepted;
D. instantiating the four editorial admins does not change the graph.

Plus the one gap this slice deliberately leaves open: ``Prompt.tags``.
"""
import json
import subprocess
import sys

import reversion
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib import admin as django_admin
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

# _get_options is reversion's own accessor for a model's registration options.
# There is no public equivalent, and reading it here is exactly what makes this
# a contract test rather than a smoke test. Production code never touches it -
# core.reversion_registration uses is_registered()/register() only.
from reversion.revisions import _get_options, _registered_models

from core.reversion_registration import (
    DEFERRED_EDITORIAL_RELATIONS,
    EDITORIAL_REVERSION_MANIFEST,
    register_editorial_reversion_models,
)


def _manifest_models():
    for app_label, model_name, follow in EDITORIAL_REVERSION_MANIFEST:
        yield django_apps.get_model(app_label, model_name), follow


def _default_registration_fields(model):
    """What ``reversion.register(model)`` derives when ``fields`` is None."""
    opts = model._meta.concrete_model._meta
    return tuple(field.name for field in opts.local_fields + opts.local_many_to_many)


# ======================================================================
# A. Startup order
# ======================================================================


STARTUP_PROBE = r"""
import json, os, sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mentoroai.settings")

# Patch before django.setup() so every register() call during startup is seen,
# whoever makes it. reversion.admin does `from reversion.revisions import
# register` at import time, and that import happens during admin autodiscovery
# - i.e. after this patch - so a VersionAdmin auto-registration would be
# recorded here with caller "reversion.admin".
import reversion
import reversion.revisions as rr

events = []
_orig_register = rr.register


def _register_spy(model=None, **kwargs):
    events.append({
        "kind": "register",
        "model": "%s.%s" % (model._meta.app_label, model._meta.model_name),
        "follow": list(kwargs.get("follow", ())),
        "caller": sys._getframe(1).f_globals.get("__name__"),
    })
    return _orig_register(model, **kwargs)


rr.register = _register_spy
reversion.register = _register_spy

import django.contrib.admin as django_admin_module

_orig_autodiscover = django_admin_module.autodiscover


def _autodiscover_spy():
    events.append({"kind": "autodiscover_start"})
    return _orig_autodiscover()


django_admin_module.autodiscover = _autodiscover_spy

import django

django.setup()

sys.stdout.write("---EVENTS---" + json.dumps(events))
"""


class EditorialReversionStartupOrderTests(SimpleTestCase):
    """
    The ordering contract, proved against a real ``django.setup()`` in a fresh
    process rather than against the already-booted registry of this test run.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        completed = subprocess.run(
            [sys.executable, "-c", STARTUP_PROBE],
            cwd=str(settings.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "startup probe failed:\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        marker = "---EVENTS---"
        assert marker in completed.stdout, completed.stdout
        cls.events = json.loads(completed.stdout.split(marker, 1)[1])

    def _registrations(self):
        return [e for e in self.events if e["kind"] == "register"]

    def test_admin_autodiscovery_actually_ran_in_the_probe(self):
        """Guards the other assertions: a probe where autodiscovery never
        happened would pass them vacuously."""
        self.assertIn(
            {"kind": "autodiscover_start"},
            self.events,
            "admin autodiscovery did not run - the probe proves nothing",
        )

    def test_every_editorial_registration_happens_before_autodiscovery(self):
        autodiscover_index = self.events.index({"kind": "autodiscover_start"})
        late = [
            e["model"]
            for e in self.events[autodiscover_index:]
            if e["kind"] == "register"
        ]
        self.assertEqual(
            late,
            [],
            "these models were registered at or after admin autodiscovery, so "
            "their configuration still depends on admin import order",
        )

    def test_no_model_is_registered_by_versionadmin_autoregistration(self):
        by_admin = [
            (e["model"], e["follow"])
            for e in self._registrations()
            if e["caller"] == "reversion.admin"
        ]
        self.assertEqual(
            by_admin,
            [],
            "VersionAdmin auto-registered these models - the Beta 11.11A race "
            "is back",
        )

    def test_central_module_is_the_only_registration_source(self):
        callers = {e["caller"] for e in self._registrations()}
        self.assertEqual(
            callers,
            {"core.reversion_registration"},
            "more than one code path registers editorial models at startup",
        )

    def test_startup_registers_exactly_the_manifest_in_order(self):
        recorded = [(e["model"], tuple(e["follow"])) for e in self._registrations()]
        expected = [
            (f"{app_label}.{model_name.lower()}", tuple(follow))
            for app_label, model_name, follow in EDITORIAL_REVERSION_MANIFEST
        ]
        self.assertEqual(recorded, expected)


# ======================================================================
# B. Exact manifest
# ======================================================================


class EditorialReversionManifestTests(SimpleTestCase):
    """The live registry matches the manifest field for field."""

    def test_every_manifest_model_is_registered(self):
        missing = [
            model._meta.label
            for model, _follow in _manifest_models()
            if not reversion.is_registered(model)
        ]
        self.assertEqual(missing, [])

    def test_follow_matches_the_manifest_for_every_model(self):
        for model, follow in _manifest_models():
            with self.subTest(model=model._meta.label):
                self.assertEqual(
                    _get_options(model).follow,
                    tuple(follow),
                    f"{model._meta.label} has the wrong follow graph",
                )

    def test_parents_follow_their_translations_and_editorial_children(self):
        """The specific relationships Beta 11.11A found missing, spelled out so
        a regression names the actual defect rather than a tuple mismatch."""
        expectations = {
            "guides.Guide": ("translations", "sections"),
            "guides.GuideSection": ("translations", "items"),
            "guides.GuideItem": ("translations",),
            "prompts.Prompt": ("translations",),
            "usecases.UseCase": ("translations",),
            "compare.Comparison": ("translations", "tool_entries"),
            "compare.ComparisonToolEntry": ("translations",),
        }
        for label, follow in expectations.items():
            app_label, model_name = label.split(".")
            model = django_apps.get_model(app_label, model_name)
            with self.subTest(model=label):
                self.assertEqual(_get_options(model).follow, follow)

    def test_translation_models_are_registered_and_follow_nothing(self):
        for label in (
            "guides.GuideTranslation",
            "guides.GuideSectionTranslation",
            "guides.GuideItemTranslation",
            "prompts.PromptTranslation",
            "usecases.UseCaseTranslation",
            "compare.ComparisonTranslation",
            "compare.ComparisonToolEntryTranslation",
        ):
            app_label, model_name = label.split(".")
            model = django_apps.get_model(app_label, model_name)
            with self.subTest(model=label):
                self.assertTrue(reversion.is_registered(model))
                self.assertEqual(_get_options(model).follow, ())

    def test_follow_names_are_real_accessors(self):
        """A typo in the manifest would otherwise only surface as silently
        missing data in a revision."""
        for model, follow in _manifest_models():
            for name in follow:
                with self.subTest(model=model._meta.label, accessor=name):
                    self.assertTrue(
                        hasattr(model, name),
                        f"{model._meta.label} has no accessor {name!r}",
                    )

    def test_serialization_options_are_unchanged_reversion_defaults(self):
        """Beta 11.11B1 changes when and what is registered, never how a row is
        serialized. No arbitrary field reduction."""
        for model, _follow in _manifest_models():
            options = _get_options(model)
            with self.subTest(model=model._meta.label):
                self.assertEqual(options.fields, _default_registration_fields(model))
                self.assertEqual(options.format, "json")
                self.assertIs(options.for_concrete_model, True)
                self.assertIs(options.ignore_duplicates, False)
                self.assertIs(options.use_natural_foreign_keys, False)

    def test_editorial_workflow_and_snapshot_fields_stay_versioned(self):
        """B1 does not narrow the payload; the later review fingerprint decides
        what counts as reviewed content."""
        for label, expected in (
            ("guides.Guide", ("status", "live_i18n", "last_published_revision_id")),
            ("compare.Comparison", ("status", "live_i18n", "live_entries")),
            ("usecases.UseCaseTranslation", ("persona",)),
        ):
            app_label, model_name = label.split(".")
            model = django_apps.get_model(app_label, model_name)
            fields = _get_options(model).fields
            for name in expected:
                with self.subTest(model=label, field=name):
                    self.assertIn(name, fields)

    def test_plain_m2m_membership_stays_on_the_parent_registration(self):
        for label, name in (
            ("guides.Guide", "categories"),
            ("guides.Guide", "tools"),
            ("prompts.Prompt", "tools"),
            ("usecases.UseCase", "tools"),
        ):
            app_label, model_name = label.split(".")
            model = django_apps.get_model(app_label, model_name)
            with self.subTest(model=label, field=name):
                self.assertIn(name, _get_options(model).fields)

    def test_no_external_catalog_model_is_registered(self):
        """Tool/Category have no editorial workflow; freezing them into a
        content revision is explicitly out of contract."""
        for label in ("catalog.Tool", "catalog.Category"):
            app_label, model_name = label.split(".")
            model = django_apps.get_model(app_label, model_name)
            with self.subTest(model=label):
                self.assertFalse(reversion.is_registered(model))

    def test_registry_contains_nothing_beyond_the_manifest(self):
        registered = {f"{app}.{model}" for app, model in _registered_models}
        expected = {
            f"{app_label}.{model_name.lower()}"
            for app_label, model_name, _follow in EDITORIAL_REVERSION_MANIFEST
        }
        self.assertEqual(
            registered,
            expected,
            "an unexpected model is registered with reversion - either add it "
            "to the manifest or remove the competing registration",
        )


# ======================================================================
# C. Fail fast, never silently accept
# ======================================================================


class EditorialReversionFailFastTests(SimpleTestCase):
    def test_running_the_registration_again_raises_improperly_configured(self):
        """The models are registered at this point, so a second call is exactly
        the "someone else got there first" situation. It must fail loudly - the
        swallowed RegistrationError it replaces is how the old divergence
        stayed invisible for so long."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            register_editorial_reversion_models()
        message = str(ctx.exception)
        self.assertIn("guides.Guide", message)
        self.assertIn("compare.Comparison", message)

    def test_the_failed_second_call_left_the_registry_untouched(self):
        for model, follow in _manifest_models():
            with self.subTest(model=model._meta.label):
                self.assertEqual(_get_options(model).follow, tuple(follow))

    def test_no_app_config_registers_editorial_models_any_more(self):
        """
        The per-app ``try: reversion.register(...) except RegistrationError:
        pass`` blocks are gone for good.

        Checked on the parsed syntax tree, not on the raw text: these modules
        still *describe* the removed construct in a comment, and a substring
        scan would flag that. The subprocess test above proves the same
        property at real startup; this one keeps the defect from being
        reintroduced in a code path that startup happens not to reach.
        """
        import ast

        import compare.apps
        import guides.apps
        import prompts.apps
        import usecases.apps

        for module in (guides.apps, prompts.apps, usecases.apps, compare.apps):
            with open(module.__file__, encoding="utf-8") as _f:
                tree = ast.parse(_f.read())
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register"
            ]
            handlers = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler)
                and "RegistrationError" in ast.dump(node.type or ast.Pass())
            ]
            with self.subTest(module=module.__name__):
                self.assertEqual(calls, [], f"{module.__name__} still calls .register()")
                self.assertEqual(
                    handlers, [], f"{module.__name__} still swallows RegistrationError"
                )


# ======================================================================
# D. Admin autodiscovery does not change the graph
# ======================================================================


class AdminInstantiationDoesNotChangeGraphTests(SimpleTestCase):
    def test_instantiating_the_editorial_admins_leaves_the_manifest_intact(self):
        from compare.admin import ComparisonAdmin
        from compare.models import Comparison
        from guides.admin import GuideAdmin
        from guides.models import Guide
        from prompts.admin import PromptAdmin
        from prompts.models import Prompt
        from usecases.admin import UseCaseAdmin
        from usecases.models import UseCase

        before = {
            model._meta.label: _get_options(model).follow
            for model, _follow in _manifest_models()
        }

        for admin_class, model in (
            (GuideAdmin, Guide),
            (PromptAdmin, Prompt),
            (UseCaseAdmin, UseCase),
            (ComparisonAdmin, Comparison),
        ):
            admin_class(model, django_admin.site)

        after = {
            model._meta.label: _get_options(model).follow
            for model, _follow in _manifest_models()
        }
        self.assertEqual(before, after)

    def test_all_four_editorial_models_are_still_registered_with_the_admin_site(self):
        from compare.models import Comparison
        from guides.models import Guide, GuideSection
        from prompts.models import Prompt
        from usecases.models import UseCase

        for model in (Guide, GuideSection, Prompt, UseCase, Comparison):
            with self.subTest(model=model._meta.label):
                self.assertTrue(django_admin.site.is_registered(model))


# ======================================================================
# Deliberate gap: taggit
# ======================================================================


class PromptTagsAreDeferredTests(TestCase):
    """
    ``Prompt.tags`` is knowingly still outside the revision graph after B1.

    This asserts the gap rather than pretending it is closed: the field is part
    of the registration's ``fields`` tuple, but Django's serializer skips m2m
    with a non-``auto_created`` through model, so no tag data ever reaches
    ``serialized_data``. See DEFERRED_EDITORIAL_RELATIONS for why closing it
    would need a model change.
    """

    def test_the_gap_is_documented_in_the_registration_module(self):
        self.assertIn(("prompts", "Prompt", "tags"), DEFERRED_EDITORIAL_RELATIONS)

    def test_tags_are_not_serialized_into_a_prompt_version(self):
        from reversion.models import Version

        from prompts.models import Prompt

        with reversion.create_revision():
            prompt = Prompt.objects.create()
            prompt.create_translation("en", title="Tagged", slug="tagged-prompt-b1")
            prompt.tags.add("alpha", "beta")
            prompt.save()

        version = (
            Version.objects.get_for_object(prompt).order_by("-pk").first()
        )
        fields = json.loads(version.serialized_data)[0]["fields"]
        self.assertNotIn("tags", fields)
        self.assertEqual(sorted(prompt.tags.names()), ["alpha", "beta"])
