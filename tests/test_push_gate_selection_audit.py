# test-path: cross-cutting
"""Ground-truth audit logic for the incremental push gate (#122).

Pins that ``audit_scope`` measures a scoped-gate MISS correctly: a FULL plan can
never miss; a scoped plan misses any whole-tree finding/failure outside its scope,
and passes when everything is inside scope. This is the anti-vacuity trust-builder
that must fail LOUD before the operator flips the flag on.
"""

import subprocess
from pathlib import Path

import pytest

from scripts.ci import push_gate_selection_audit as audit
from scripts.ci.push_gate_selection_audit import audit_scope
from teatree.quality.push_gate import WHOLE_TREE_DOCTEST, PushGatePlan

_FULL = PushGatePlan(
    is_full=True, reason="full", doctest_targets=(WHOLE_TREE_DOCTEST,), astgrep_scope=None, enabled=True
)
_SCOPED = PushGatePlan(
    is_full=False,
    reason="scoped",
    doctest_targets=(Path("src/teatree/a.py"),),
    astgrep_scope=(Path("src/teatree/a.py"), Path("tests/teatree_x/test_a.py")),
    enabled=True,
)


class TestAuditScope:
    def test_full_plan_never_misses(self) -> None:
        assert audit_scope(_FULL, ["src/teatree/z.py"], ["src/teatree/z.py"]) == []

    def test_astgrep_finding_outside_scope_is_a_miss(self) -> None:
        misses = audit_scope(_SCOPED, ["src/teatree/z.py"], [])
        assert len(misses) == 1
        assert misses[0].dimension == "ast-grep"
        assert misses[0].path == "src/teatree/z.py"

    def test_astgrep_finding_inside_scope_is_not_a_miss(self) -> None:
        assert audit_scope(_SCOPED, ["src/teatree/a.py"], []) == []

    def test_doctest_failure_outside_scope_is_a_miss(self) -> None:
        misses = audit_scope(_SCOPED, [], ["src/teatree/z.py"])
        assert len(misses) == 1
        assert misses[0].dimension == "doctest"

    def test_doctest_failure_inside_scope_is_not_a_miss(self) -> None:
        assert audit_scope(_SCOPED, [], ["src/teatree/a.py"]) == []

    def test_clean_whole_tree_passes(self) -> None:
        assert audit_scope(_SCOPED, [], []) == []


def _sweep_result(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["pytest"], returncode=returncode, stdout=stdout, stderr="")


class TestWholeTreeDoctestSweepFailsLoud:
    """An empty failure set only means "clean tree" when the sweep actually ran.

    A collection error, an internal error, or a run that collected nothing all
    produce no parseable ``FAILED`` line — indistinguishable from a green sweep,
    so the audit would certify the scoping it never measured.
    """

    @pytest.mark.parametrize("returncode", [2, 3, 4, 5])
    def test_infrastructure_exit_code_raises(
        self,
        returncode: int,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(audit, "run_allowed_to_fail", lambda *a, **k: _sweep_result(returncode, ""))

        with pytest.raises(SystemExit) as exc_info:
            audit._whole_tree_doctest_failures(tmp_path)

        assert str(returncode) in str(exc_info.value)

    def test_reported_failures_that_parse_to_nothing_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            audit,
            "run_allowed_to_fail",
            lambda *a, **k: _sweep_result(1, "1 failed in 0.10s\n"),
        )

        with pytest.raises(SystemExit):
            audit._whole_tree_doctest_failures(tmp_path)

    def test_green_sweep_returns_no_failures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(audit, "run_allowed_to_fail", lambda *a, **k: _sweep_result(0, "42 passed in 1.0s\n"))

        assert audit._whole_tree_doctest_failures(tmp_path) == []

    def test_parsed_failures_are_returned_sorted_and_deduped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        stdout = "FAILED src/teatree/z.py::z\nFAILED src/teatree/a.py::a\nFAILED src/teatree/z.py::z2\n"
        monkeypatch.setattr(audit, "run_allowed_to_fail", lambda *a, **k: _sweep_result(1, stdout))

        assert audit._whole_tree_doctest_failures(tmp_path) == ["src/teatree/a.py", "src/teatree/z.py"]
