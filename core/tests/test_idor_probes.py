"""
Beta 11.12D2: the bounded IDOR probe set keeps its security content while
losing its dependency on the global primary-key sequence.

Two contracts are pinned here:

A. :func:`core.tests.idor_probes.build_bounded_idor_probe_ids` returns a small,
   fixed, deduplicated set that always contains the foreign draft, its
   neighbourhood and a provably absent id, and never the requester's own
   allowed id - and whose size does not grow when the ids do.
B. No test module anywhere in the project iterates a ``range()`` whose bound is
   derived from a ``.pk``/``.id`` again. That is the actual regression D2
   fixes: such a loop makes runtime a function of how many rows earlier tests
   happened to create.

Contract B is checked over the AST, not over the file text, so a docstring
that *describes* the old ``range(1, foreign.pk + 2)`` pattern - as several of
these modules now do - can never make the guard fail on itself.
"""
import ast
import pathlib

from django.conf import settings
from django.test import SimpleTestCase

from core.tests.idor_probes import (
    ABSENT_ID_OFFSET,
    MAX_PROBE_IDS,
    build_bounded_idor_probe_ids,
)

REPO_ROOT = pathlib.Path(settings.BASE_DIR)
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


def _test_modules():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(REPO_ROOT)
        if "tests" in relative.parts or path.name == "tests.py":
            yield path


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


# ======================================================================
# A. The probe set itself
# ======================================================================


class BoundedProbeSetTests(SimpleTestCase):
    """``own_id=10``/``foreign_id=11`` mirrors the real fixture shape: two
    drafts created back to back, so their neighbourhoods overlap."""

    OWN = 10
    FOREIGN = 11
    EXISTING = (10, 11)

    def probes(self, **overrides):
        kwargs = {
            "own_id": self.OWN,
            "foreign_id": self.FOREIGN,
            "existing_ids": self.EXISTING,
        }
        kwargs.update(overrides)
        return build_bounded_idor_probe_ids(**kwargs)

    def test_the_set_never_exceeds_the_documented_maximum(self):
        self.assertLessEqual(len(self.probes()), MAX_PROBE_IDS)

    def test_the_size_is_independent_of_how_large_the_ids_are(self):
        """The regression in one assertion: a sequence that has advanced into
        the millions must not produce a single extra request."""
        small = build_bounded_idor_probe_ids(
            own_id=11, foreign_id=12, existing_ids=(11, 12)
        )
        huge = build_bounded_idor_probe_ids(
            own_id=12_000_000, foreign_id=12_000_001, existing_ids=(12_000_000, 12_000_001)
        )
        self.assertEqual(len(small), len(huge))
        self.assertLessEqual(len(huge), MAX_PROBE_IDS)

    def test_the_foreign_draft_is_always_probed(self):
        self.assertIn(self.FOREIGN, self.probes())

    def test_the_requesters_own_allowed_id_is_never_probed(self):
        self.assertNotIn(self.OWN, self.probes())

    def test_the_own_id_stays_excluded_even_when_it_is_a_neighbour(self):
        """``own_id`` sits exactly at ``foreign_id - 1`` in the real fixtures,
        so the neighbour rule would otherwise re-introduce it."""
        probes = build_bounded_idor_probe_ids(
            own_id=41, foreign_id=42, existing_ids=(41, 42)
        )
        self.assertNotIn(41, probes)
        self.assertIn(42, probes)

    def test_both_neighbourhoods_are_covered(self):
        probes = build_bounded_idor_probe_ids(
            own_id=100, foreign_id=200, existing_ids=(100, 200)
        )
        for expected in (99, 101, 199, 201):
            with self.subTest(probe=expected):
                self.assertIn(expected, probes)

    def test_zero_is_probed_because_the_route_accepts_it(self):
        self.assertIn(0, self.probes())

    def test_a_provably_absent_high_id_is_probed(self):
        probes = self.probes()
        highest_existing = max(self.EXISTING)
        self.assertTrue(
            any(probe > highest_existing for probe in probes),
            "no probe above the highest existing row",
        )
        self.assertIn(highest_existing + ABSENT_ID_OFFSET, probes)

    def test_the_absent_id_respects_rows_that_are_not_the_two_drafts(self):
        """The absent probe is derived from *every* known row, not just from
        the two drafts, so a fixture with higher ids cannot make it collide."""
        probes = build_bounded_idor_probe_ids(
            own_id=10, foreign_id=11, existing_ids=(10, 11, 5000)
        )
        self.assertIn(5000 + ABSENT_ID_OFFSET, probes)

    def test_the_set_contains_no_duplicates(self):
        probes = self.probes()
        self.assertEqual(len(probes), len(set(probes)))

    def test_no_negative_id_is_produced(self):
        probes = build_bounded_idor_probe_ids(
            own_id=0, foreign_id=1, existing_ids=(0, 1)
        )
        self.assertTrue(all(probe >= 0 for probe in probes))

    def test_the_result_is_deterministic_and_sorted(self):
        first = self.probes()
        second = self.probes()
        self.assertEqual(first, second)
        self.assertEqual(list(first), sorted(first))

    def test_existing_ids_may_be_any_iterable_without_being_consumed_twice(self):
        probes = build_bounded_idor_probe_ids(
            own_id=10, foreign_id=11, existing_ids=[10, 11]
        )
        self.assertIn(11, probes)


# ======================================================================
# B. No unbounded pk-derived range survives anywhere in the test suite
# ======================================================================


class NoPrimaryKeyDerivedRangeTests(SimpleTestCase):
    def test_no_test_module_iterates_a_range_bound_to_a_pk_or_id(self):
        offenders = []
        for path in _test_modules():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:  # pragma: no cover - unparsable file would fail elsewhere
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _dotted(node.func) != "range":
                    continue
                referenced = {
                    inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)
                }
                if referenced & {"pk", "id"}:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} {ast.unparse(node)}"
                    )
        self.assertEqual(offenders, [])

    def test_the_guard_actually_inspects_the_draft_preview_modules(self):
        """Guards the guard: a scan that silently found no files would pass
        the assertion above vacuously."""
        scanned = {str(path.relative_to(REPO_ROOT)) for path in _test_modules()}
        for expected in (
            "prompts/tests/test_draft_preview_permissions.py",
            "guides/tests/test_draft_preview_security.py",
            "usecases/tests/test_draft_preview_permissions.py",
            "compare/tests/test_draft_preview_permissions.py",
        ):
            with self.subTest(module=expected):
                self.assertIn(expected, scanned)

    def test_every_draft_preview_idor_test_uses_the_bounded_helper(self):
        """The four modules must reach their probe ids through the shared
        helper - an inline replacement loop would satisfy the AST guard above
        while quietly re-inventing an unbounded candidate set."""
        modules = (
            "prompts/tests/test_draft_preview_permissions.py",
            "guides/tests/test_draft_preview_security.py",
            "usecases/tests/test_draft_preview_permissions.py",
            "compare/tests/test_draft_preview_permissions.py",
        )
        missing = []
        for relative in modules:
            tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
            calls = {
                _dotted(node.func)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            }
            if "build_bounded_idor_probe_ids" not in calls:
                missing.append(relative)
        self.assertEqual(missing, [])
