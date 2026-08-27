# test-path: cross-cutting — walks every hooks/scripts module; no single-module mirror.
"""Conformance guards for the one seam that invokes the ``t3`` CLI from a hook.

A hook runs as a bare ``python3`` subprocess started in the SESSION's working
directory, and the containerized ``t3`` entry point refuses a directory it cannot
see from inside the container. A hook that shells out with an inherited cwd is
therefore refused for a reason unrelated to what it asked — and a gate that fails
CLOSED turns that refusal into a DENY that blocks correct work.

That bug was found and fixed INLINE at one call site, then rediscovered
independently at another. These lanes exist so it cannot be rediscovered a third
time, by making "did not use the seam" a static, mechanical failure instead of an
incident:

* **Single resolver** — only :mod:`hooks.scripts.t3_invocation` looks the ``t3``
    binary up. A hook that calls ``shutil.which("t3")`` itself has, by
    construction, built an argv the seam never sees.
* **Single runner** — a ``t3`` argv is handed to the seam's runners, never to a
    raw ``subprocess`` call, because a raw call is exactly where the ``cwd``
    keyword goes missing.
* **The seam pins a cwd** — every subprocess the seam itself spawns passes
    ``cwd=``, so routing through it actually buys the guarantee it advertises.

Static and tree-wide on purpose: the failure this prevents is one of OMISSION, and
an omission has no call site to unit-test.
"""

import ast
from pathlib import Path

import pytest

from tests.conformance._src_tree import REPO_ROOT, parsed_modules

_HOOK_SCRIPTS_DIR = REPO_ROOT / "hooks" / "scripts"

#: The seam itself — the one module allowed to resolve and spawn ``t3`` directly.
_SEAM_MODULE = "t3_invocation.py"

#: The seam's public entry points, by role.
_ARGV_BUILDER = "t3_argv"
_SEAM_RUNNERS = frozenset({"run_t3", "spawn_t3_detached"})

#: ``subprocess`` members that start a process.
_SUBPROCESS_RUNNERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

_T3 = "t3"


def _hook_modules() -> tuple[tuple[Path, ast.Module], ...]:
    """Every ``hooks/scripts`` module except the seam, parsed once per process."""
    return tuple(item for item in parsed_modules(_HOOK_SCRIPTS_DIR) if item[0].name != _SEAM_MODULE)


def _seam_module() -> ast.Module:
    """The seam's own AST — the lanes below assert against it directly."""
    for path, tree in parsed_modules(_HOOK_SCRIPTS_DIR):
        if path.name == _SEAM_MODULE:
            return tree
    pytest.fail(f"the t3 invocation seam is missing: expected {_HOOK_SCRIPTS_DIR / _SEAM_MODULE}")


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _resolves_t3_binary(node: ast.AST) -> bool:
    """Whether *node* is a ``shutil.which("t3")`` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "which"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "shutil"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _T3
    )


def _subprocess_runner_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``subprocess.<runner>(...)`` call in *tree*."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SUBPROCESS_RUNNERS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]


def _bindings(tree: ast.Module) -> list[tuple[set[str], ast.expr]]:
    """Every ``name = <expr>`` in *tree*, as ``(bound names, value)``."""
    pairs: list[tuple[set[str], ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            continue
        value = node.value
        if value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if names:
            pairs.append((names, value))
    return pairs


def _t3_bearing_names(tree: ast.Module) -> set[str]:
    """Names whose value carries a ``t3`` invocation, closed over re-binding.

    Seeded from the two ways a ``t3`` argv comes into existence — the seam's
    builder, and the hand-rolled ``shutil.which("t3")`` it replaces — then grown to
    a fixpoint so ``argv = [t3_bin, "loop", ...]`` is caught as readily as the
    binary it was built from. Covering the hand-rolled shape too is what makes this
    lane catch the defect BEFORE the seam exists, not merely protect it after.
    """
    pairs = _bindings(tree)
    bearing: set[str] = set()
    while True:
        grown = {
            name
            for names, value in pairs
            if _mentions_argv_builder(value) or _resolves_t3_binary_anywhere(value) or _mentions_any(value, bearing)
            for name in names
        }
        if grown <= bearing:
            return bearing
        bearing |= grown


def _mentions_argv_builder(node: ast.AST) -> bool:
    """Whether *node* contains a call to the seam's argv builder."""
    return any(
        isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == _ARGV_BUILDER
        for inner in ast.walk(node)
    )


def _resolves_t3_binary_anywhere(node: ast.AST) -> bool:
    """Whether *node* contains a ``shutil.which("t3")`` call."""
    return any(_resolves_t3_binary(inner) for inner in ast.walk(node))


def _mentions_any(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(inner, ast.Name) and inner.id in names for inner in ast.walk(node))


def _carries_t3_argv(node: ast.AST, argv_names: set[str]) -> bool:
    """Whether the argv expression *node* carries a ``t3`` invocation.

    Four shapes, covering how an argv actually reaches a runner: a literal whose
    leader is ``"t3"``, an inline resolution, the seam builder's result used
    inline, and a name any of those were bound to.
    """
    if _mentions_argv_builder(node) or _resolves_t3_binary_anywhere(node) or _mentions_any(node, argv_names):
        return True
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        leader = node.elts[0]
        return isinstance(leader, ast.Constant) and leader.value == _T3
    return False


class TestOnlyTheSeamResolvesT3:
    """A hook that looks ``t3`` up itself has built an argv the seam never sees."""

    def test_no_hook_module_resolves_the_t3_binary_itself(self) -> None:
        offenders = [
            f"{_rel(path)}:{node.lineno}"
            for path, tree in _hook_modules()
            for node in ast.walk(tree)
            if _resolves_t3_binary(node)
        ]
        assert not offenders, (
            "these hook modules resolve the `t3` binary themselves instead of asking the seam:\n  "
            + "\n  ".join(offenders)
            + f"\nUse `{_ARGV_BUILDER}(...)` / `t3_available()` from hooks.scripts.{_SEAM_MODULE[:-3]} — a hand-built "
            "argv is how a `t3` shell-out ends up with no cwd and gets refused by the containerized entry point."
        )


class TestT3ArgvReachesOnlyTheSeamRunners:
    """A raw ``subprocess`` call is exactly where the ``cwd`` keyword goes missing."""

    def test_no_hook_module_spawns_a_t3_argv_directly(self) -> None:
        offenders: list[str] = []
        for path, tree in _hook_modules():
            argv_names = _t3_bearing_names(tree)
            offenders += [
                f"{_rel(path)}:{call.lineno}"
                for call in _subprocess_runner_calls(tree)
                if call.args and _carries_t3_argv(call.args[0], argv_names)
            ]
        assert not offenders, (
            "these hook modules hand a `t3` argv straight to subprocess:\n  "
            + "\n  ".join(offenders)
            + f"\nRoute it through {sorted(_SEAM_RUNNERS)} instead — they pin a cwd the container can see, "
            "which a raw call silently inherits from the harness session directory."
        )


class TestTheSeamPinsACwd:
    """Routing through the seam has to actually buy the guarantee it advertises."""

    def test_every_subprocess_the_seam_spawns_passes_a_cwd(self) -> None:
        offenders = [
            f"{_SEAM_MODULE}:{call.lineno}"
            for call in _subprocess_runner_calls(_seam_module())
            if not any(keyword.arg == "cwd" for keyword in call.keywords)
        ]
        assert not offenders, (
            "the t3 seam spawns a process without pinning a cwd:\n  "
            + "\n  ".join(offenders)
            + "\nAn omitted cwd inherits the harness session directory — the whole defect the seam exists to close."
        )

    def test_the_seam_exposes_the_runners_the_other_lanes_name(self) -> None:
        defined = {
            node.name for node in ast.walk(_seam_module()) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert defined >= _SEAM_RUNNERS, (
            f"the seam is missing {sorted(_SEAM_RUNNERS - defined)} — the other lanes point callers at runners "
            "that must exist for their advice to be followable."
        )
