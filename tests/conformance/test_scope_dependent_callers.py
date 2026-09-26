"""Scope-dependent readers must be exercised WITH the scope open, not only bare.

The defect class: a function whose behaviour is decided by ambient state an
enclosing context manager installs. Called bare it takes the default branch and
behaves correctly; reached through its real caller while the scope is open it
takes the other branch, which is where the bug lives. Every direct-call test
passes, and the production path is broken — the tests cannot see the branch they
were written to guard.

Teatree's instance of the shape is the request-scoped memo: ``request_scope()``
sets the ``_ACTIVE`` contextvar and ``@cached_per_request`` reads it, so a
decorated read returns a MEMOIZED value inside the scope and a fresh one outside.
A test that only calls such a read bare exercises the pass-through branch; the
staleness branch — the one that serves a value the caller has since invalidated —
is never entered.

This lane derives both sides from the tree, never a hand-list:

*   a **scope opener** is a ``@contextmanager`` that calls ``<contextvar>.set(…)``;
*   a **scope reader** is a top-level function that calls ``<contextvar>.get(…)``,
    or one decorated by a function that does;
*   a reader is **covered** when some test function that references it also has
    an opener entered — in its own body, or in a fixture it names.

A reader that tests exercise ONLY bare is named. A reader no test touches at all
is a different defect (untested code) and is not this lane's business.

**What is NOT checkable here.** "Behaviour depends on an enclosing scope" has no
general syntactic marker — an open transaction, a patched module, a frozen clock
and an env var are all enclosing scopes with nothing in the callee naming them.
So this lane is scoped to the one mechanism that DOES name itself in the source,
the contextvar/context-manager pair. Ambient state a callee reads through any
other channel stays invisible to it.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

from tests.conformance._src_tree import REPO_ROOT, SRC_DIR, parsed_modules, src_modules

_TESTS_DIR = REPO_ROOT / "tests"

_CONTEXTVAR_FACTORY = "ContextVar"
_CONTEXTMANAGER_DECORATORS = frozenset({"contextmanager", "asynccontextmanager"})

# Scope-dependent readers whose only tests call them BARE. Each entry is a standing
# admission that the memoized branch of that read is unguarded — the sibling staleness
# test refuses an entry that is no longer a reader or that HAS gained coverage, so the
# list drains rather than accumulating, and a NEW reader is not on it and fails here
# until it earns a scope-open test. ``get_effective_settings`` was drained first
# (``tests/test_request_cache.py::TestARealMemoizedRead``).
READERS_EXERCISED_ONLY_BARE: frozenset[str] = frozenset(
    {
        "build_report",
        "manual_override_map",
        "discover_active_overlay",
        "discover_overlays",
        "effective_verdicts",
        "resolve_active_mode",
        "timer_chain_loop_names",
    }
)


@dataclass(frozen=True, slots=True)
class ScopeAwareTest:
    """One ``def test_*`` and what it can see: the names it references, and whether a scope is open."""

    module: str
    name: str
    references: frozenset[str]
    scope_open: bool


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name is not None:
            names.add(name)
    return names


def _attribute_calls_on(tree: ast.AST, holders: frozenset[str], attribute: str) -> bool:
    """True when *tree* calls ``<holder>.<attribute>(…)`` for any name in *holders*."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in holders
        for node in ast.walk(tree)
    )


def context_vars_in(tree: ast.Module) -> frozenset[str]:
    """Module-level ``ContextVar`` names — the ambient state a scope installs."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if isinstance(node.value, ast.Call) and _call_name(node.value) == _CONTEXTVAR_FACTORY:
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names)


def _top_level_functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


@cache
def scope_openers() -> dict[str, str]:
    """``opener -> rel-file`` for every context manager that SETS a scope contextvar."""
    openers: dict[str, str] = {}
    for path, tree in src_modules():
        holders = context_vars_in(tree)
        if not holders:
            continue
        for node in _top_level_functions(tree):
            if _decorator_names(node) & _CONTEXTMANAGER_DECORATORS and _attribute_calls_on(node, holders, "set"):
                openers[node.name] = str(path.relative_to(SRC_DIR))
    return openers


@cache
def scope_readers() -> dict[str, str]:
    """``reader -> rel-file`` for every function a scope changes the behaviour of.

    Direct readers call ``<contextvar>.get(…)``; a direct reader used as a decorator
    makes every function it decorates a reader too, which is how the memoized reads
    enrol without naming the contextvar themselves.
    """
    direct: dict[str, str] = {}
    for path, tree in src_modules():
        holders = context_vars_in(tree)
        if not holders:
            continue
        for node in _top_level_functions(tree):
            if _attribute_calls_on(node, holders, "get"):
                direct[node.name] = str(path.relative_to(SRC_DIR))

    decorated: dict[str, str] = {}
    for path, tree in src_modules():
        for node in _top_level_functions(tree):
            if _decorator_names(node) & direct.keys():
                decorated[node.name] = str(path.relative_to(SRC_DIR))
    return direct | decorated


def _fixtures_opening_a_scope(tree: ast.Module, openers: frozenset[str]) -> set[str]:
    return {
        node.name
        for node in _top_level_functions(tree)
        if "fixture" in _decorator_names(node) and _enters_a_scope(node, openers)
    }


def _enters_a_scope(node: ast.AST, openers: frozenset[str]) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call) and _call_name(item.context_expr) in openers
        for sub in ast.walk(node)
        if isinstance(sub, ast.With | ast.AsyncWith)
        for item in sub.items
    )


@cache
def exercised_tests() -> tuple[ScopeAwareTest, ...]:
    """Every ``def test_*`` under ``tests/``, with the names it references and its scope state."""
    openers = frozenset(scope_openers())
    shared = _fixtures_opening_a_scope(_conftest_tree(), openers)
    found: list[ScopeAwareTest] = []
    for path, tree in parsed_modules(_TESTS_DIR):
        module = str(path.relative_to(_TESTS_DIR))
        local = _fixtures_opening_a_scope(tree, openers) | shared
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not node.name.startswith("test_"):
                continue
            parameters = {argument.arg for argument in node.args.args}
            found.append(
                ScopeAwareTest(
                    module=module,
                    name=node.name,
                    references=frozenset(_referenced_names(node)),
                    scope_open=_enters_a_scope(node, openers) or bool(parameters & local),
                )
            )
    return tuple(found)


@cache
def _conftest_tree() -> ast.Module:
    return ast.parse((_TESTS_DIR / "conftest.py").read_text(encoding="utf-8"))


def _referenced_names(node: ast.AST) -> Iterator[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr


# ── Pure predicates (shared by the real assertions and the anti-vacuity self-tests) ──


def readers_exercised_only_bare(
    readers: dict[str, str], tests: tuple[ScopeAwareTest, ...], allow: frozenset[str]
) -> list[str]:
    """Readers some test references, but which no test references with a scope open."""
    bare: set[str] = set()
    under_scope: set[str] = set()
    for test in tests:
        touched = test.references & readers.keys()
        (under_scope if test.scope_open else bare).update(touched)
    return sorted(bare - under_scope - allow)


def bare_call_sites(readers: list[str], tests: tuple[ScopeAwareTest, ...]) -> dict[str, str]:
    """``reader -> one test that calls it bare`` — where the missing coverage is owed."""
    sites: dict[str, str] = {}
    for test in tests:
        if test.scope_open:
            continue
        for reader in test.references & set(readers):
            sites.setdefault(reader, f"{test.module}::{test.name}")
    return sites


class TestEveryExercisedScopeReaderIsExercisedUnderScope:
    """A reader every test calls bare has its scope-dependent branch unguarded."""

    def test_no_scope_reader_is_exercised_only_by_a_direct_call(self) -> None:
        uncovered = readers_exercised_only_bare(scope_readers(), exercised_tests(), READERS_EXERCISED_ONLY_BARE)
        assert not uncovered, (
            "scope-dependent reader(s) that every test calls BARE — the branch a scope opens is "
            "unguarded, so a defect in it passes every direct test: "
            f"{bare_call_sites(uncovered, exercised_tests())}. Give one such test a "
            f"`with <{'/'.join(sorted(scope_openers()))}>():` around the call, or allowlist it on purpose."
        )

    def test_allowlisted_readers_are_still_readers_and_still_uncovered(self) -> None:
        readers = scope_readers()
        stale = sorted(name for name in READERS_EXERCISED_ONLY_BARE if name not in readers)
        assert not stale, f"READERS_EXERCISED_ONLY_BARE entries that are no longer scope readers: {stale}"

        now_covered = sorted(
            set(READERS_EXERCISED_ONLY_BARE) - set(readers_exercised_only_bare(readers, exercised_tests(), frozenset()))
        )
        assert not now_covered, f"allowlisted readers that NOW have scope-open coverage (drop them): {now_covered}"


class TestScopeDependenceIsDerivedFromTheTree:
    """Anti-vacuity — a broken derivation that discovers nothing must not pass green."""

    def test_the_request_scope_opener_is_discovered(self) -> None:
        assert scope_openers().get("request_scope") == "request_cache.py", scope_openers()

    def test_the_memoized_reads_enrol_through_their_decorator(self) -> None:
        readers = scope_readers()
        assert "cached_per_request" in readers, sorted(readers)
        assert readers.get("get_effective_settings") == "config/resolution.py", sorted(readers)

    def test_reader_and_test_indexes_are_densely_populated(self) -> None:
        assert len(scope_readers()) >= 10, sorted(scope_readers())
        assert len(exercised_tests()) >= 2000


_SYNTHETIC_READERS = {"synthetic_reader": "synthetic.py", "other_reader": "synthetic.py"}


def _synthetic_test(*, references: set[str], scope_open: bool) -> ScopeAwareTest:
    return ScopeAwareTest("test_synthetic.py", "test_it", frozenset(references), scope_open)


class TestScopeCoverageWalkFiresRed:
    """Anti-vacuity — the predicate must NAME a bare-only reader, not silently pass."""

    def test_a_reader_only_ever_called_bare_is_named(self) -> None:
        tests = (
            _synthetic_test(references={"synthetic_reader"}, scope_open=False),
            _synthetic_test(references={"other_reader"}, scope_open=True),
        )
        assert readers_exercised_only_bare(_SYNTHETIC_READERS, tests, frozenset()) == ["synthetic_reader"]

    def test_one_scope_open_test_clears_the_reader(self) -> None:
        tests = (
            _synthetic_test(references={"synthetic_reader"}, scope_open=False),
            _synthetic_test(references={"synthetic_reader"}, scope_open=True),
        )
        assert readers_exercised_only_bare(_SYNTHETIC_READERS, tests, frozenset()) == []

    def test_an_allowlisted_reader_is_not_named(self) -> None:
        tests = (_synthetic_test(references={"synthetic_reader"}, scope_open=False),)
        allow = frozenset({"synthetic_reader"})
        assert readers_exercised_only_bare(_SYNTHETIC_READERS, tests, allow) == []

    def test_the_failure_message_points_at_a_bare_call_site(self) -> None:
        tests = (_synthetic_test(references={"synthetic_reader"}, scope_open=False),)
        assert bare_call_sites(["synthetic_reader"], tests) == {"synthetic_reader": "test_synthetic.py::test_it"}

    def test_a_reader_no_test_touches_is_not_this_lanes_business(self) -> None:
        tests = (_synthetic_test(references={"unrelated"}, scope_open=False),)
        assert readers_exercised_only_bare(_SYNTHETIC_READERS, tests, frozenset()) == []

    def test_the_scope_probe_can_tell_an_open_scope_from_a_bare_call(self) -> None:
        openers = frozenset({"request_scope"})
        opened = ast.parse("def test_it():\n    with request_scope():\n        read()\n")
        bare = ast.parse("def test_it():\n    read()\n")
        assert _enters_a_scope(opened, openers)
        assert not _enters_a_scope(bare, openers)
