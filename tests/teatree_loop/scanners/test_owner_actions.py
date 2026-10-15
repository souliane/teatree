"""The global tick carries out answered action cards before pending tasks."""

from unittest.mock import patch

from teatree.loop.domain_jobs import _global_dispatch_jobs
from teatree.loop.scanners.owner_actions import OwnerActionScanner


def test_global_dispatch_orders_owner_actions_before_pending_tasks() -> None:
    with (
        patch("teatree.loop.domain_jobs.messaging_from_overlay", return_value=None),
        patch("teatree.loop.domain_jobs.resolve_user_id", return_value=""),
    ):
        names = [job.scanner.name for job in _global_dispatch_jobs()]
    assert names.index("owner_actions") < names.index("pending_tasks")


def test_scanner_emits_one_signal_per_carried_out_card() -> None:
    with patch("teatree.loop.scanners.owner_actions.carry_out_owner_actions", return_value=["carried out 7"]):
        signals = OwnerActionScanner().scan()
    assert [(signal.kind, signal.summary) for signal in signals] == [("owner_action.carried_out", "carried out 7")]
