"""Diff-scoped CI lane classifier with fail-safe-unknown routing (#132).

Reads the set of paths a PR changed and decides which CI lanes must run.
A single ``preflight`` job runs this and exports the booleans; every
downstream job gates on the matching output via ``needs:`` + ``if:``.

Safety doctrine (a wrongly-skipped lane is a false green — the worst
outcome, so skipping is conservative):

-   FAIL-SAFE-UNKNOWN: any path this classifier does not positively
    recognise as docs, python, or config forces ``all`` — run everything.
    Uncertainty never skips.
-   The ONLY sanctioned skip: a provably pure-docs/markdown diff (only
    ``*.md`` / ``docs/**``, with no python, no config, no CI/rule change)
    may skip the HEAVY python lanes (``test``, ``mutation-diff``). It still
    runs every docs/markdown gate and every always-on security lane.
-   Any ``*.py`` / ``*.pyi`` change forces every python lane. A non-python
    file under a code directory (a fixture, a binary asset) is NOT
    recognised as python — it falls through to ``all`` (which still runs
    every python lane), so the code lane is never wrongly skipped.
-   Any config / CI / ast-grep rule / lockfile / Dockerfile change forces
    ``all`` — those can affect any job, so none may be skipped.
-   Security / quality lanes (regression-rules, banned-terms, sbom,
    uv-audit) and the docs gates run on EVERY classification — never skipped.

The classifier itself classifies the changed PATHS; the ``preflight``
job feeds it the result of ``git diff --name-only base...HEAD``.
"""

import json
import pathlib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Hoisted into ``teatree.quality.changed_set`` so the push gate (#122) and this
# CI-lane classifier share ONE definition and never drift (architecture check #8);
# ``tests/quality/test_changed_set_classifier.py`` pins the two modules agree.
from teatree.quality.changed_set import CONFIG_EXACT as _CONFIG_EXACT
from teatree.quality.changed_set import CONFIG_PREFIXES as _CONFIG_PREFIXES
from teatree.quality.changed_set import CONFIG_SUFFIXES as _CONFIG_SUFFIXES

LANE_TEST = "test"
LANE_LINT = "lint"
LANE_TEST_SHAPE = "test-shape"
LANE_REGRESSION_RULES = "regression-rules"
LANE_MUTATION_DIFF = "mutation-diff"
LANE_DOCS_DRIFT = "docs-drift"
LANE_DOC_UPDATE = "doc-update-gate"
LANE_BLUEPRINT_CROSS_PR = "blueprint-cross-pr"
LANE_COMMENT_DENSITY = "comment-density-warning"
LANE_SBOM = "sbom"
LANE_UV_AUDIT = "uv-audit"

HEAVY_PYTHON_LANES: frozenset[str] = frozenset({LANE_TEST, LANE_MUTATION_DIFF})
PYTHON_LANES: frozenset[str] = frozenset(
    {LANE_TEST, LANE_LINT, LANE_TEST_SHAPE, LANE_REGRESSION_RULES, LANE_MUTATION_DIFF}
)
DOCS_LANES: frozenset[str] = frozenset(
    {LANE_DOCS_DRIFT, LANE_DOC_UPDATE, LANE_BLUEPRINT_CROSS_PR, LANE_COMMENT_DENSITY}
)
SECURITY_LANES: frozenset[str] = frozenset({LANE_REGRESSION_RULES, LANE_SBOM, LANE_UV_AUDIT})

_DOCS_SUFFIXES: tuple[str, ...] = (".md", ".markdown")
_DOCS_PREFIXES: tuple[str, ...] = ("docs/",)
_PYTHON_SUFFIXES: tuple[str, ...] = (".py", ".pyi")


@dataclass(frozen=True)
class Lanes:
    """Which CI lanes the changed diff requires.

    ``all`` is the dominant flag: when ``True`` every lane runs and the
    individual ``run_*`` properties all report ``True``. The individual
    flags only narrow the run when ``all`` is ``False`` (the pure-docs
    skip). ``run_security`` is unconditionally ``True`` — security and
    quality gates are never skipped on any diff.
    """

    all: bool = False
    _run_heavy_python: bool = False
    _run_python: bool = False
    _run_docs: bool = False

    @property
    def run_heavy_python(self) -> bool:
        return self.all or self._run_heavy_python

    @property
    def run_python(self) -> bool:
        return self.all or self._run_python

    @property
    def run_docs(self) -> bool:
        return self.all or self._run_docs

    @property
    def run_security(self) -> bool:
        return True

    def as_outputs(self) -> dict[str, bool]:
        return {
            "all": self.all,
            "run_heavy_python": self.run_heavy_python,
            "run_python": self.run_python,
            "run_docs": self.run_docs,
            "run_security": self.run_security,
        }


def _is_docs(path: str) -> bool:
    return path.endswith(_DOCS_SUFFIXES) or path.startswith(_DOCS_PREFIXES)


def _is_python(path: str) -> bool:
    # Extension-only by design: a non-.py file under a code dir is left
    # unrecognised so it fails safe to all=True, never a narrow code lane.
    return path.endswith(_PYTHON_SUFFIXES)


def _is_config(path: str) -> bool:
    if path in _CONFIG_EXACT or path.startswith(_CONFIG_PREFIXES):
        return True
    return path.endswith(_CONFIG_SUFFIXES)


def classify(paths: Iterable[str]) -> Lanes:
    """Classify changed ``paths`` into the lanes that must run.

    Order of dominance (most conservative wins):

    1.  An empty diff or any unrecognised path → ``all`` (fail-safe).
    2.  Any config / CI / ast-grep rule / lockfile path → ``all``.
    3.  Any python / code path → every python lane (+ docs + security).
    4.  A pure-docs diff → docs + security only (heavy python skipped).
    """
    cleaned = [p.strip() for p in paths if p.strip()]
    if not cleaned:
        return Lanes(all=True)

    saw_python = False
    saw_docs = False

    for path in cleaned:
        if _is_config(path):
            return Lanes(all=True)
        if _is_python(path):
            saw_python = True
            continue
        if _is_docs(path):
            saw_docs = True
            continue
        # Unrecognised path: never skip on uncertainty.
        return Lanes(all=True)

    if saw_python:
        return Lanes(_run_heavy_python=True, _run_python=True, _run_docs=True)

    if saw_docs:
        return Lanes(_run_docs=True)

    return Lanes(all=True)


def _read_paths(argv: Sequence[str]) -> list[str]:
    if argv:
        return list(argv)
    return [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]


def main(argv: Sequence[str] | None = None, output_path: str | None = None) -> int:
    paths = _read_paths([] if argv is None else list(argv))
    lanes = classify(paths)
    outputs = lanes.as_outputs()

    if output_path:
        with pathlib.Path(output_path).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{key}={'true' if value else 'false'}\n" for key, value in outputs.items())

    print(json.dumps(outputs))
    return 0


if __name__ == "__main__":
    import os

    raise SystemExit(main(sys.argv[1:], output_path=os.environ.get("GITHUB_OUTPUT")))
