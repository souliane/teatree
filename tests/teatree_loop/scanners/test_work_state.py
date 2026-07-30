"""Tests for the WorkStateScanner (SELFCATCH-1).

Exercises the signal translation and fail-closed boundary against a patched
``reconcile_work_state_all``, and pins the scanner's registration + routing so a
finding surfaces in the action-needed statusline zone rather than staying silent.
"""

from pathlib import Path
from unittest.mock import patch

from teatree.core.worktree.reconcile import DoneButUnmerged, Drift, DuplicateScope, UnpushedWork
from teatree.loop.dispatch import dispatch
from teatree.loop.domain_jobs import _global_dispatch_jobs
from teatree.loop.scanners import ScanSignal, WorkStateScanner

_MOD = "teatree.core.worktree.reconcile.reconcile_work_state_all"


def _drift(**kwargs: object) -> Drift:
    return Drift(ticket_pk=1, **kwargs)


class TestWorkStateScannerSignals:
    def test_no_drift_yields_no_signals(self) -> None:
        with patch(_MOD, return_value={}):
            assert WorkStateScanner().scan() == []

    def test_unpushed_work_emits_workstate_drift(self) -> None:
        drift = _drift(unpushed_work=[UnpushedWork(worktree_pk=5, branch="feature", shas=["abc123 feat: x"])])
        with patch(_MOD, return_value={1: drift}):
            signals = WorkStateScanner().scan()
        assert [s.kind for s in signals] == ["workstate.drift"]
        assert signals[0].payload["finding"] == "unpushed_work"
        assert signals[0].payload["worktree_pk"] == 5

    def test_inconclusive_probe_finding_is_surfaced(self) -> None:
        drift = _drift(unpushed_work=[UnpushedWork(worktree_pk=5, branch="feature", probe_error="git boom")])
        with patch(_MOD, return_value={1: drift}):
            signals = WorkStateScanner().scan()
        assert signals[0].kind == "workstate.drift"
        assert "inconclusive" in signals[0].summary

    def test_done_but_unmerged_and_duplicate_scope_each_emit(self) -> None:
        drift = _drift(
            done_but_unmerged=[DoneButUnmerged(ticket_pk=1, branch="feature", reason="no merge audit")],
            duplicate_scopes=[DuplicateScope(issue_number="42", paths=[Path("/w/42-a"), Path("/w/42-b")])],
        )
        with patch(_MOD, return_value={1: drift}):
            signals = WorkStateScanner().scan()
        findings = {s.payload["finding"] for s in signals}
        assert findings == {"done_but_unmerged", "duplicate_scope"}

    def test_errored_sweep_fails_closed_to_a_finding(self) -> None:
        with patch(_MOD, side_effect=RuntimeError("db down")):
            signals = WorkStateScanner().scan()
        assert [s.kind for s in signals] == ["workstate.probe_error"]
        assert "db down" in signals[0].summary


class TestWorkStateScannerRegistration:
    def test_registered_in_the_global_dispatch_set(self) -> None:
        names = {job.scanner.name for job in _global_dispatch_jobs()}
        assert "work_state" in names

    def test_drift_signal_routes_to_action_needed(self) -> None:
        signal = ScanSignal(kind="workstate.drift", summary="unpushed work on feature", payload={})
        actions = dispatch([signal])
        assert [(a.kind, a.zone) for a in actions] == [("statusline", "action_needed")]

    def test_probe_error_routes_to_action_needed(self) -> None:
        signal = ScanSignal(kind="workstate.probe_error", summary="reconcile failed", payload={})
        actions = dispatch([signal])
        assert [(a.kind, a.zone) for a in actions] == [("statusline", "action_needed")]
