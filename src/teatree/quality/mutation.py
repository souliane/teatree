"""Scoped (narrow) mutation-testing registry and diff-scoping logic.

Mutation testing exposes vacuous coverage: a surviving mutant is a line a test
exercises but does not actually assert on. It is expensive, so teatree keeps it
NARROW — applied only to a hand-picked set of high-value SAFETY modules where a
silent regression (a fail-closed gate that stops failing closed, a lost-update
in a claim/CAS path, a merge-clear that stops gating) is worst. The registry is
``[tool.teatree.mutation].high_value_modules`` in ``pyproject.toml`` (mirrors
``[tool.teatree.coverage]`` and ``[tool.teatree.test_shape]``).

``scope_modules`` is the cheap gate: it intersects the PR's changed files with
the registry. When no safety module is touched the intersection is empty and the
run no-ops, so most PRs pay nothing. The runner that drives mutmut over the
scoped subset lives in :mod:`teatree.quality.mutation_run`.
"""

import tomllib
from collections.abc import Iterable
from pathlib import Path

_SECTION = "[tool.teatree.mutation]"


class MutationConfigError(ValueError):
    """Raised when the mutation registry in ``pyproject.toml`` is malformed."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{_SECTION} {detail}")


def registry_pyproject_path() -> Path:
    """Path to the repo ``pyproject.toml`` that carries the registry."""
    return Path(__file__).resolve().parents[3] / "pyproject.toml"


def load_high_value_modules(pyproject_path: Path | None = None) -> tuple[str, ...]:
    """Return the declared high-value safety modules (repo-relative ``src/`` paths).

    Validates the registry exists, is a non-empty list of strings. Each entry is
    a path like ``src/teatree/on_behalf_gate.py``.
    """
    path = pyproject_path or registry_pyproject_path()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    section = data.get("tool", {}).get("teatree", {}).get("mutation")
    if section is None:
        detail = "section is absent"
        raise MutationConfigError(detail)
    modules = section.get("high_value_modules")
    if not isinstance(modules, list) or any(not isinstance(m, str) for m in modules):
        detail = "high_value_modules must be a list of strings"
        raise MutationConfigError(detail)
    if not modules:
        detail = "high_value_modules must be non-empty (narrow, not empty)"
        raise MutationConfigError(detail)
    return tuple(modules)


def scope_modules(changed_files: Iterable[str], *, registry: Iterable[str]) -> tuple[str, ...]:
    """Return the registry entries present in *changed_files*, in registry order.

    The diff ∩ registry intersection. Empty result ⇒ no safety module was
    touched ⇒ the mutation run no-ops. Registry order is preserved so the
    output is deterministic regardless of diff ordering.
    """
    changed = set(changed_files)
    return tuple(module for module in registry if module in changed)
