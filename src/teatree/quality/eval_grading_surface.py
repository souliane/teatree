"""The ``teatree.eval`` modules that participate in computing a scenario verdict.

A hand-maintained list of a computed surface drifts: the eval-heal anti-cheat
gate shipped with four grading modules named and ``report.py`` — the module that
computes ``ScenarioResult.passed`` — admitted by omission (#4220). This walk
derives the surface instead, as the transitive import closure of the modules
that decide pass/fail, so the gate can be CHECKED against the real call graph
rather than trusted to have listed it.

Pure AST over the package's own sources: nothing is imported, so a walk over a
half-broken tree still answers, and the result does not depend on what happens
to be importable in the running venv. The closure is restricted to
``teatree.eval`` — a grader's edge into ``teatree.utils`` reaches product code
the healer is *supposed* to fix, and following it would deny the whole repo.

Fail-loud, never fail-safe: a seed that no longer exists RAISES. Degrading to a
smaller surface would leave the conformance check green while certifying
nothing, which is the failure this module exists to prevent.
"""

import ast
from collections.abc import Iterable
from pathlib import Path

#: The package the closure is restricted to — a dotted prefix, not a filesystem path.
EVAL_PACKAGE = "teatree.eval"

#: The modules that COMPUTE a verdict, seeding the reachability walk. Each is an
#: entry point into grading, not the surface itself: ``report`` evaluates a
#: scenario, ``loader`` turns ``expect:`` into the matchers it is evaluated over,
#: ``triage`` classifies a red, ``judge`` folds in the LLM verdict, ``skip_guard``
#: refuses a vacuous green, and ``summary_json`` / ``green_proof`` are the
#: artifacts every downstream consumer reads the verdict from.
GRADING_SEEDS: tuple[str, ...] = (
    "report.py",
    "loader.py",
    "triage.py",
    "judge.py",
    "skip_guard.py",
    "summary_json.py",
    "green_proof.py",
)


class MissingGradingSeedError(RuntimeError):
    """A declared grading seed is absent — the surface would silently shrink."""


def _module_file(package_dir: Path, dotted_suffix: str) -> Path | None:
    parts = dotted_suffix.split(".")
    for candidate in (package_dir.joinpath(*parts).with_suffix(".py"), package_dir.joinpath(*parts, "__init__.py")):
        if candidate.is_file():
            return candidate
    return None


def _imported_modules(source: str, package_dir: Path) -> set[Path]:
    suffixes: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == EVAL_PACKAGE:
            suffixes.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(f"{EVAL_PACKAGE}."):
            suffixes.add(node.module.removeprefix(f"{EVAL_PACKAGE}."))
        elif isinstance(node, ast.Import):
            suffixes.update(
                alias.name.removeprefix(f"{EVAL_PACKAGE}.")
                for alias in node.names
                if alias.name.startswith(f"{EVAL_PACKAGE}.")
            )
    resolved = (_module_file(package_dir, suffix) for suffix in suffixes)
    return {path for path in resolved if path is not None}


def grading_surface(package_dir: Path, *, seeds: Iterable[str] = GRADING_SEEDS) -> frozenset[Path]:
    """Every module in *package_dir* reachable from *seeds* by an intra-package import.

    Raises :class:`MissingGradingSeedError` if a seed is not a file under
    *package_dir* — a renamed grader must be redeclared, not silently dropped.
    """
    pending: list[Path] = []
    for seed in seeds:
        path = package_dir / seed
        if not path.is_file():
            message = (
                f"grading seed {seed!r} is not a file under {package_dir} — "
                "redeclare it in GRADING_SEEDS so the surface stays complete"
            )
            raise MissingGradingSeedError(message)
        pending.append(path)

    reached: set[Path] = set()
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        reached.add(module)
        pending.extend(_imported_modules(module.read_text(), package_dir) - reached)
    return frozenset(reached)
