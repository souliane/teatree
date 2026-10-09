"""``runs_full_suite`` rejects whole-``testpaths`` runs, allows scoped ones.

Moved verbatim from ``tests/test_no_full_suite_on_pre_push.py::TestFullSuiteMatcher`` when
the matcher was extracted to ``src`` so a second consumer could share it
(souliane/teatree#3994). The bug it guards against is a trailing slash (``pytest tests/``)
sneaking the full suite past the gate.
"""

import time
import tomllib
from pathlib import Path

import pytest

from teatree.quality.full_suite_invocation import declared_testpaths, runs_full_suite

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_TESTPATHS_ROOTS = declared_testpaths(_PYPROJECT)


class TestFullSuiteMatcher:
    @pytest.mark.parametrize(
        "invocation",
        [
            "pytest",
            "pytest tests",
            "pytest tests/",
            "pytest ./tests/",
            'pytest "tests/"',
            "pytest 'tests/'",
            "uv run pytest tests/",
            "python -m pytest tests",
            "uv run pytest tests//",
            'pytest -m "not push_heavy"',
        ],
    )
    def test_whole_suite_invocations_are_rejected(self, invocation: str) -> None:
        assert runs_full_suite(invocation, _TESTPATHS_ROOTS), f"{invocation!r} runs the whole suite but was not caught"

    @pytest.mark.parametrize(
        "invocation",
        [
            "pytest tests/quality",
            "pytest tests/test_gate_never_lockout_contract.py",
            "pytest tests/test_gate_never_lockout_contract.py::TestGate::test_x",
            'pytest tests/quality -m "not push_heavy"',
            'uv run pytest tests/quality tests/test_gate_never_lockout_contract.py -m "not push_heavy" -q',
            "pytest src/",
            "bash dev/push-gate.sh",
            "uv run t3 tool push-gate --run",
        ],
    )
    def test_scoped_invocations_are_allowed(self, invocation: str) -> None:
        assert not runs_full_suite(invocation, _TESTPATHS_ROOTS), (
            f"{invocation!r} is genuinely scoped and must be allowed"
        )

    @pytest.mark.parametrize(
        "invocation",
        [
            "pytest --basetemp /tmp/pt",
            "uv run pytest --junitxml build/report.xml",
            "uv run pytest --cov-report term-missing",
            "pytest --basetemp /tmp/pt -q",
        ],
    )
    def test_an_option_value_is_never_read_as_a_scoped_path(self, invocation: str) -> None:
        # Every one of these collects the whole testpaths tree; the token after the
        # option is its value, and reading it as a positional path hid the full run.
        assert runs_full_suite(invocation, _TESTPATHS_ROOTS), f"{invocation!r} runs the whole suite but was not caught"

    def test_commented_invocation_is_not_an_invocation(self) -> None:
        # A `#`-commented mention documents the forbidden shape; it is not a run.
        assert not runs_full_suite("# never do `uv run pytest tests/` on the push path", _TESTPATHS_ROOTS)
        # ... but an inline comment must not mask a real run earlier on the line.
        assert runs_full_suite("uv run pytest tests/  # early full-suite signal", _TESTPATHS_ROOTS)

    def test_unbalanced_quote_falls_back_to_whitespace_split(self) -> None:
        # shlex raises on the apostrophe; the fallback must still see the invocation
        # rather than drop the line (a dropped line is a silent miss, not a safe skip).
        assert runs_full_suite("it's the full suite: uv run pytest", _TESTPATHS_ROOTS)

    def test_forbidden_root_is_derived_from_testpaths(self) -> None:
        # No hardcoded copy of "tests": the root tracks pyproject, so a rename
        # cannot leave the guard pinned to a stale directory name.
        declared = tomllib.loads(_PYPROJECT.read_text())["tool"]["pytest"]["ini_options"]["testpaths"]
        assert tuple(declared) == _TESTPATHS_ROOTS
        assert _TESTPATHS_ROOTS, "testpaths must be non-empty or the guard has no root to forbid"

    def test_matcher_is_redos_bounded(self) -> None:
        # This runs inside a git hook: a pathological argument must not wedge it.
        # The old backtracking lookahead was the exact ReDoS shape this pins shut.
        pathological = "pytest " + "tests/" * 10_000
        start = time.perf_counter()
        runs_full_suite(pathological, _TESTPATHS_ROOTS)
        assert time.perf_counter() - start < 0.5, "matcher is not linear -- possible ReDoS regression"
