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

Beta 11.12D2A gates that AST check behind a cheap ``tokenize``-based
pre-filter (:func:`_has_executable_range_name`): most project test files never
mention ``range`` at all, so there is no reason to ``ast.parse()`` them. The
filter only ever *widens* the candidate set (an independent audit confirmed
zero missed files against every real ``range()`` call in the project - see
:func:`_pk_bound_range_violations`); the AST rule itself, and what counts as a
violation, is unchanged.
"""
import ast
import io
import pathlib
import tempfile
import tokenize
from unittest import mock

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


def _has_executable_range_name(source):
    """
    Beta 11.12D2A Stage 1: does ``source`` contain the identifier ``range`` as
    a real token - outside any comment or string literal?

    Deliberately a *token* check, not a substring one: ``tokenize`` represents
    an entire comment or an entire string/docstring as a single COMMENT/STRING
    token and never re-tokenizes its text, so a docstring describing
    ``range(1, object.pk)`` tokenizes with zero NAME tokens equal to
    ``"range"`` - a plain ``"range(" in source`` search cannot make that
    distinction.

    Whatever :func:`tokenize.generate_tokens` itself cannot lex propagates out
    of this function unchanged; nothing here decides a file "is not a
    candidate" because reading it failed.
    """
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return any(tok.type == tokenize.NAME and tok.string == "range" for tok in tokens)


def _pk_bound_range_violations(path):
    """
    The Beta 11.12D2 AST rule, gated by :func:`_has_executable_range_name`: a
    file is only ``ast.parse``'d if its own tokens contain ``range`` at all.
    Every ``range(...)`` call in a parsed file whose arguments reference a
    ``.pk`` or ``.id`` attribute, anywhere in the call's own subtree, is
    reported as ``"file:line source"``.

    A genuine ``SyntaxError`` from ``ast.parse`` on a promoted candidate
    propagates unchanged - never swallowed as if the file were clean.
    """
    source = path.read_text(encoding="utf-8", errors="ignore")
    if not _has_executable_range_name(source):
        return []

    try:
        filename = str(path.relative_to(REPO_ROOT))
    except ValueError:
        filename = str(path)

    tree = ast.parse(source, filename=filename)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _dotted(node.func) != "range":
            continue
        referenced = {
            inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)
        }
        if referenced & {"pk", "id"}:
            violations.append(f"{filename}:{node.lineno} {ast.unparse(node)}")
    return violations


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
        """
        Beta 11.12D2A: gated by :func:`_has_executable_range_name`, so only
        files that actually mention ``range`` are ``ast.parse``'d - an
        independent audit found 48 of the project's 246 test files do (none
        of them a violation), down from parsing all 246 directly.
        """
        offenders = []
        for path in _test_modules():
            offenders.extend(_pk_bound_range_violations(path))
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


# ======================================================================
# Beta 11.12D2A: the cheap token pre-filter in front of the AST rule above
# ======================================================================


class RangeTokenFilterTests(SimpleTestCase):
    """Pins Stage 1 directly, independent of the slow project-wide scan."""

    def test_an_executable_range_call_is_a_token_candidate(self):
        sources = {
            "plain": "for value in range(10):\n    pass\n",
            "space_before_parenthesis": "for value in range (1, object.pk):\n    pass\n",
            "multiline": "values = list(range(\n    0,\n    object.id + 2,\n))\n",
        }
        for label, source in sources.items():
            with self.subTest(label=label):
                self.assertTrue(_has_executable_range_name(source))

    def test_comments_docstrings_and_string_literals_are_not_token_candidates(self):
        sources = {
            "comment": "# range(1, object.pk)\n",
            "docstring": '"""Never use range(1, object.pk)."""\n',
            "string_literal": 'text = "range(1, object.pk)"\n',
        }
        for label, source in sources.items():
            with self.subTest(label=label):
                self.assertFalse(_has_executable_range_name(source))


class PkBoundRangeDetectionTests(SimpleTestCase):
    """Pins Stage 2 and its gating, using real temp files (never committed
    to the repository)."""

    def _write(self, tmp, name, content):
        path = pathlib.Path(tmp) / name
        path.write_text(content)
        return path

    def test_a_pk_bound_range_in_a_candidate_file_is_reported_with_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._write(
                tmp, "bad.py", "for pk in range(1, object.pk + 2):\n    pass\n"
            )
            violations = _pk_bound_range_violations(bad)
        self.assertEqual(len(violations), 1)
        self.assertIn(f"{bad}:1", violations[0])
        self.assertIn("range(1, object.pk + 2)", violations[0])

    def test_ast_parse_is_only_called_for_token_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self._write(tmp, "clean.py", "def test_a(self):\n    pass\n")
            candidate = self._write(
                tmp, "candidate.py", "for pk in range(1, object.pk):\n    pass\n"
            )

            with self.subTest("no range token -> never parsed"):
                with mock.patch("ast.parse", wraps=ast.parse) as parse:
                    self.assertEqual(_pk_bound_range_violations(clean), [])
                parse.assert_not_called()

            with self.subTest("range token -> parsed"):
                with mock.patch("ast.parse", wraps=ast.parse) as parse:
                    _pk_bound_range_violations(candidate)
                parse.assert_called_once()


class SyntaxAndTokenErrorsPropagateTests(SimpleTestCase):
    def test_a_tokenization_error_propagates(self):
        with self.assertRaises((tokenize.TokenError, IndentationError, SyntaxError)):
            _has_executable_range_name('x = "unterminated\n')

    def test_a_syntax_error_in_a_candidate_file_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp) / "broken.py"
            broken.write_text(
                "def test(self):\n    for pk in range(1, object.pk)\n        pass\n"
            )
            with self.assertRaises(SyntaxError):
                _pk_bound_range_violations(broken)
