"""The CLEAR reconciler is wired into the loop, not merely defined (#4250).

Reclassifying a merged PR out of the doctor FAIL only moves a permanent finding one
severity down — the row still stands unconsumed and every backlog surface still names
it. Convergence needs the reconciler to RUN unattended, so an unwired reconciler leaves
the ticket's actual complaint (73 unconsumed authorisations, nothing reporting them)
exactly where it was. Same for the self-improve detector's forge reader: its default is
the fail-safe ``unverified_reader``, so a detector nobody arms reports nothing at all.

The #3984 intake-budget adapter is pinned here too: it moved modules in this change and
had no test of its own, so nothing would have caught the move dropping its call.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata
from teatree.loop.reconcile_lanes import reconcile_holder_pr_rows_best_effort
from teatree.loop.scanner_factories import _pr_sweep_scanner_for
from teatree.loop.self_improve.detectors import ForgottenMergeDetector
from teatree.loop.self_improve.schedule import _cheap_detectors

_RECONCILE = "teatree.core.merge.clear_reconcile.reconcile_settled_clears"
_HOLDER_RECONCILE = "teatree.core.intake.budget.reconcile_holder_pr_rows"


def _backend(name: str = "t3-teatree") -> OverlayBackends:
    config = MagicMock(spec=OverlayConfig)
    config.get_github_token = lambda: ""
    metadata = MagicMock(spec=OverlayMetadata)
    metadata.get_followup_repos = lambda: ["acme/repo"]
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = config
    overlay.metadata = metadata
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        overlay=overlay,
        identities=(),
    )


class TestSweepLaneWiring(TestCase):
    @pytest.fixture(autouse=True)
    def _muzzle_entry_points(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])

    def test_the_sweep_lane_reconciles_settled_clears(self) -> None:
        with patch(_RECONCILE) as reconcile:
            assert _pr_sweep_scanner_for(_backend(), slack_user_id="") is not None

        assert reconcile.call_count == 1
        assert reconcile.call_args.kwargs["read_state"] is not None

    def test_a_failing_reconcile_never_stops_the_sweep_being_built(self) -> None:
        with patch(_RECONCILE, side_effect=RuntimeError("forge down")):
            assert _pr_sweep_scanner_for(_backend(), slack_user_id="") is not None


class TestHolderReconcileSurvivedTheMove(TestCase):
    """The #3984 intake reconcile still runs — it moved modules in this change, untested."""

    @pytest.fixture(autouse=True)
    def _muzzle_entry_points(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])

    def test_the_adapter_still_reaches_the_budget_reconcile(self) -> None:
        with patch(_HOLDER_RECONCILE) as reconcile:
            reconcile_holder_pr_rows_best_effort("t3-teatree")

        assert reconcile.call_args.args == ("t3-teatree",)
        assert reconcile.call_args.kwargs["read_state"] is not None

    def test_a_failing_reconcile_never_propagates(self) -> None:
        with patch(_HOLDER_RECONCILE, side_effect=RuntimeError("forge down")):
            reconcile_holder_pr_rows_best_effort("t3-teatree")


class TestDetectorReaderWiring(TestCase):
    def test_the_cheap_tier_arms_the_forgotten_merge_reader(self) -> None:
        detector = next(d for d in _cheap_detectors() if isinstance(d, ForgottenMergeDetector))

        assert detector.read_state is not ForgottenMergeDetector().read_state
        assert detector.read_state.__name__ == "pr_open_state"
