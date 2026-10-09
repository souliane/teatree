"""Domain names under ``src`` that pytest would collect must opt out of collection.

pytest collects any module-level class matching ``python_classes`` (``Test*``)
and any module-level function matching ``python_functions`` (``test*``) from a
module it is handed. The scoped doctest step of the push gate hands it every
changed ``src`` module, so a domain class named ``TestPlanWrite`` — nothing to do
with tests — is collected, fails on its ``__init__``, and the whole step errors.
The blast radius is every IMPORTER too, because the imported name is a
module-level attribute of the importing module as well: ``_test_plan/write.py``
alone errored 28 times, blocking any diff that touched it.

Two cures satisfy the walk. ``__test__ = False`` in the class body fixes it at
the definition for every importer at once, and is what the ``Test*`` domain
classes carry. A TypedDict body and a function object accept no such marker
under ``ty`` (``invalid-typed-dict-statement`` / ``unresolved-attribute``), so
those names are renamed out of the collection globs instead.

This lane is the ratchet that keeps it fixed — the next domain concept named
``TestSomething`` fails here instead of silently re-blocking the gate. The
synthetic lanes prove the walk is anti-vacuous: RED on the exact bug shape, GREEN
on the opted-out form.
"""

import ast
import tomllib
from pathlib import Path

from teatree.core.management.commands._test_plan.write import TestPlanWrite
from teatree.utils.django_db.testdb_clone import TestDbCloneResult
from tests.conformance._src_tree import REPO_ROOT, src_modules

# Importing the real names at module scope makes THIS module the canary: each is
# a module-level attribute here too, so a regressed opt-out errors this lane's
# own collection — the exact failure the guard describes.

# pytest's DEFAULT collection globs. ``TestPytestDefaultsAreNotOverridden`` below
# pins that this repo does not narrow or widen them, so the walk cannot drift
# away from what pytest actually collects.
_CLASS_PREFIX = "Test"
_FUNCTION_PREFIX = "test"

_OPT_OUT = "__test__"


def _assign_targets(stmt: ast.stmt) -> tuple[ast.expr, ...]:
    """The assignment targets of *stmt*, covering the bare and annotated forms alike.

    Both spellings are live in the tree — ``__test__ = False`` in a plain class and
    ``__test__: ClassVar[bool] = False`` in a dataclass/enum — so a walk that reads
    only ``ast.Assign`` reports an already-fixed class as a violation.
    """
    if isinstance(stmt, ast.Assign):
        return tuple(stmt.targets)
    if isinstance(stmt, ast.AnnAssign):
        return (stmt.target,)
    return ()


def _class_opts_out(node: ast.ClassDef) -> bool:
    """True when *node*'s body assigns ``__test__`` (the pytest opt-out marker)."""
    return any(
        isinstance(target, ast.Name) and target.id == _OPT_OUT for stmt in node.body for target in _assign_targets(stmt)
    )


def _function_opt_outs(tree: ast.Module) -> set[str]:
    """Every function name carrying a module-level ``<name>.__test__ = ...`` assignment."""
    return {
        target.value.id
        for stmt in tree.body
        for target in _assign_targets(stmt)
        if isinstance(target, ast.Attribute) and target.attr == _OPT_OUT and isinstance(target.value, ast.Name)
    }


def _is_collected(node: ast.stmt, opted_out: frozenset[str]) -> bool:
    """True when pytest would collect *node* as a test and nothing opted it out."""
    if isinstance(node, ast.ClassDef):
        return node.name.startswith(_CLASS_PREFIX) and not _class_opts_out(node)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name.startswith(_FUNCTION_PREFIX) and node.name not in opted_out
    return False


def collectable_names(tree: ast.Module) -> list[tuple[str, int]]:
    """``(name, lineno)`` for every module-level definition pytest would collect as a test."""
    opted_out = frozenset(_function_opt_outs(tree))
    return [
        (node.name, node.lineno)
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and _is_collected(node, opted_out)
    ]


def _scan(source: str) -> list[str]:
    return [name for name, _lineno in collectable_names(ast.parse(source))]


class TestLiveTreeOptsOut:
    def test_no_src_name_is_collectable_without_opting_out(self) -> None:
        violations = [
            f"  {path.relative_to(REPO_ROOT)}:{lineno} {name}"
            for path, tree in src_modules()
            for name, lineno in collectable_names(tree)
        ]
        assert not violations, (
            "pytest would collect these src definitions as tests, erroring the scoped "
            f"doctest step for every module that imports them — add `{_OPT_OUT} = False`:\n" + "\n".join(violations)
        )


class TestAntiVacuity:
    """The walk must go RED on the #4167 shape and GREEN on the opted-out form."""

    def test_domain_class_named_test_is_flagged(self) -> None:
        assert _scan("class TestPlanWrite:\n    pass\n") == ["TestPlanWrite"]

    def test_domain_class_with_opt_out_is_clean(self) -> None:
        assert _scan("class TestPlanWrite:\n    __test__ = False\n") == []

    def test_domain_class_with_annotated_opt_out_is_clean(self) -> None:
        assert _scan("class TestShapeReport:\n    __test__: ClassVar[bool] = False\n") == []

    def test_domain_function_named_test_is_flagged(self) -> None:
        assert _scan("def test_plan_marker(*, ticket_id):\n    return ticket_id\n") == ["test_plan_marker"]

    def test_domain_function_with_opt_out_is_clean(self) -> None:
        assert (
            _scan("def test_plan_marker(*, ticket_id):\n    return ticket_id\n\n\ntest_plan_marker.__test__ = False\n")
            == []
        )

    def test_async_domain_function_is_flagged(self) -> None:
        assert _scan("async def tests_for(module):\n    return module\n") == ["tests_for"]

    def test_unrelated_names_are_clean(self) -> None:
        assert _scan("class PlanPost:\n    pass\n\n\ndef latest_marker():\n    return 1\n") == []

    def test_nested_definition_is_out_of_scope(self) -> None:
        # pytest collects module-level attributes only; a nested class is invisible to it.
        assert _scan("class Outer:\n    class TestInner:\n        pass\n") == []

    def test_opt_out_on_another_name_does_not_clear_it(self) -> None:
        source = "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\ntest_a.__test__ = False\n"
        assert _scan(source) == ["test_b"]


class TestPytestDefaultsAreNotOverridden:
    """The walk hardcodes pytest's default globs; a repo override would invalidate it."""

    def test_repo_does_not_override_the_collection_globs(self) -> None:
        ini = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        options = ini["tool"]["pytest"]["ini_options"]
        overridden = {key for key in ("python_classes", "python_functions") if key in options}
        assert not overridden, (
            f"pyproject overrides {sorted(overridden)}; update this lane's prefixes to match "
            "or the guard no longer describes what pytest collects"
        )


class TestScanRootIsTheWholePackage:
    def test_walk_covers_more_than_one_subpackage(self) -> None:
        # Self-completeness: a re-narrowed src walk would silence the guard.
        subpackages = {path.relative_to(REPO_ROOT / "src" / "teatree").parts[0] for path, _tree in src_modules()}
        assert {"core", "quality", "utils"} <= subpackages


class TestOptOutSurvivesTheRealClasses:
    """The opt-out must be visible to pytest at RUNTIME, not just present in the AST."""

    def test_dataclass_and_enum_carry_the_runtime_marker(self) -> None:
        # A slotted dataclass and an Enum both rebuild their class dict; the marker must survive it.
        assert TestPlanWrite.__test__ is False
        assert TestDbCloneResult.__test__ is False


class TestGuardModuleIsItselfExempt:
    def test_conformance_lane_lives_outside_the_scanned_root(self) -> None:
        # This module's own Test* classes ARE real tests; the walk must never reach them.
        assert Path(__file__).resolve() not in {path for path, _tree in src_modules()}
