# test-path: cross-cutting — a fitness test over hooks/scripts/, which has no src/teatree/ mirror.
"""Extraction out of the router must be SUBTRACTIVE, not duplicative.

``hook_router`` is shrink-only and 11x over the module-health LOC cap, so logic
keeps being split into siblings. Each split has been copying the helper at its
then-current correctness instead of importing it back, and the copies then drift
apart with no arbiter:

The transcript turn-boundary walk existed in ``question_gates`` (fixed to walk
past a ``tool_result`` entry) and again in ``turn_inspect`` (not fixed), which
left the consideration Stop gate inert on every tool-using turn. The
commit-target resolver existed as ``_commit_repo_dir.resolve_commit_dir`` (used
by three gates) and again as a bare ambient-cwd read in the unknown-repo push
gate, which classified the session's repo rather than the pushed one.

These two assertions are the arbiter the copies lacked. They read source, not
behaviour, because the failure mode is a NEW copy — which by construction no
behavioural test covers yet.
"""

import ast
from pathlib import Path

import pytest

_HOOK_SCRIPTS = Path(__file__).resolve().parent.parent / "hooks" / "scripts"

#: The gates that decide WHICH repo a git command writes to. Each must ask the
#: one canonical static resolver. The roster is explicit because reading the
#: ambient cwd is legitimate elsewhere — the banned-terms and quote gates pass it
#: as an INPUT to a resolver rather than using it as the answer.
_GIT_TARGET_GATES: tuple[str, ...] = (
    "main_clone_guard.py",
    "single_branch_repo_guard.py",
    "headless_authoring_gate.py",
    "unknown_repo_push_gate.py",
)


def _hook_modules() -> list[Path]:
    return sorted(_HOOK_SCRIPTS.rglob("*.py"))


def _names_used(node: ast.AST) -> set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)} | {
        sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)
    }


def _is_turn_boundary_walk(func: ast.FunctionDef) -> bool:
    """Whether *func* walks a transcript newest→oldest and STOPS at a ``user`` entry.

    The three marks together: a ``reversed(...)`` loop, a comparison against the
    ``"user"`` role, and a ``break`` that ends the turn there. A phase-partition
    walk with role cursors (no ``break``) is a different shape and is not claimed.
    """
    has_reversed_loop = any(
        isinstance(sub, ast.For)
        and isinstance(sub.iter, ast.Call)
        and isinstance(sub.iter.func, ast.Name)
        and sub.iter.func.id == "reversed"
        for sub in ast.walk(func)
    )
    compares_user = any(
        isinstance(sub, ast.Constant) and sub.value == "user" for sub in ast.walk(func) if isinstance(sub, ast.Constant)
    )
    breaks = any(isinstance(sub, ast.Break) for sub in ast.walk(func))
    return has_reversed_loop and compares_user and breaks


def _turn_boundary_walks() -> list[tuple[Path, ast.FunctionDef]]:
    found: list[tuple[Path, ast.FunctionDef]] = []
    for path in _hook_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and _is_turn_boundary_walk(node)
        ]
    return found


class TestOneTurnBoundaryPredicate:
    def test_the_walks_exist_at_all(self) -> None:
        """Guards the detector itself: an assertion over an empty set proves nothing."""
        assert _turn_boundary_walks(), "no turn-boundary walk found — the AST detector no longer matches"

    def test_every_walk_consumes_the_shared_tool_result_predicate(self) -> None:
        offenders = [
            f"{path.name}:{func.name}"
            for path, func in _turn_boundary_walks()
            if "is_tool_result_only" not in _names_used(func)
        ]
        assert not offenders, (
            "a transcript walk ends the turn at a `user` entry without asking "
            f"`question_gates.is_tool_result_only`: {offenders}. A tool RESULT is a `user` "
            "entry, so such a walk cuts the turn at its first tool call and projects nothing."
        )


class TestOneCommitTargetResolver:
    @pytest.mark.parametrize("module", _GIT_TARGET_GATES)
    def test_git_target_gate_uses_the_canonical_resolver(self, module: str) -> None:
        source = (_HOOK_SCRIPTS / module).read_text(encoding="utf-8")
        assert "resolve_commit_dir" in source, (
            f"{module} decides which repo a git command writes to, so it must resolve that dir "
            "through `teatree.hooks._commit_repo_dir.resolve_commit_dir` — the ambient cwd is the "
            "SESSION's repo, and a session commits and pushes elsewhere all day."
        )
