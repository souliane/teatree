"""Each kind of factory chatter the 2026-10-05 audit found reaches the owner as no DM, in every drain (#5096).

The kinds are the audit's own: a wedged phase, a round ceiling, a gate hold, a provisioning failure, a red pull
request halt, a self-improve alarm and the waiting digest. Each runs through its public producer; the first-post,
resurface and re-ask drains then walk the whole queue beside one genuine owner decision that must still post.
"""

import datetime as dt
from collections.abc import Callable
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.management.commands.loop_self_improve import _deliver_owner_alert
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion
from teatree.core.models.waiting_item import WaitingItem
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
)
from teatree.loop.scanners.waiting_digest import WaitingDigestScanner
from teatree.loop.self_improve import ActionRung, DetectorReport
from tests._owner_channel import owner_card
from tests.teatree_core.test_operational_questions_never_reach_owner import (
    _fail_a_provision,
    _hold_a_dirty_own_pr_review,
    _hold_a_stalled_pr,
    _hold_past_the_rework_cap,
    _wedge_a_finished_phase,
)

#: The audit's chatter kind, and the public producer that raised it.
CHATTER: dict[str, Callable[[], object]] = {
    "wedged phase": _wedge_a_finished_phase,
    "round ceiling": _hold_past_the_rework_cap,
    "gate hold": _hold_a_dirty_own_pr_review,
    "provisioning failure": _fail_a_provision,
    "red pull request halt": _hold_a_stalled_pr,
    "waiting digest": lambda: (WaitingItem.objects.add("chase finance"), WaitingDigestScanner().scan()),
}


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D0DEMOOWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1700000000000100"
    return backend


def _alarm(kind: str) -> DetectorReport:
    return DetectorReport(
        detector="dispatch_gap",
        dedup_key="dispatch_gap::global",
        state_hash="h1",
        severity="warn",
        max_rung=ActionRung.SLACK,
        requested_rung=ActionRung.SLACK,
        summary="test report",
        payload={"kind": kind},
    )


class TestNoChatterKindReachesTheOwner(TestCase):
    def test_every_drain_posts_only_the_genuine_decision_beside_each_chatter_kind(self) -> None:
        for kind, produce in CHATTER.items():
            with self.subTest(kind=kind):
                produce()
        control = DeferredQuestion.record(
            "Which credential source should the eval run use?",
            card=owner_card(OwnerDecision.CREDENTIALS, "No eligible account is configured."),
        )
        backend = _backend()
        later = timezone.now() + dt.timedelta(hours=3)

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
            drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
            reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend, now=later)

        posted = " ".join(call.kwargs["text"] for call in backend.post_message.call_args_list)
        assert control.question in posted
        for row in DeferredQuestion.internal_pending():
            assert row.question[:40] not in posted

    def test_a_self_improve_alarm_no_signal_registers_never_posts_to_the_owner_dm(self) -> None:
        backend = _backend()

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch.object(notify_module, "resolve_user_id", return_value="U0DEMOOWNER"),
        ):
            _deliver_owner_alert(_alarm("dispatch_gap"), None)

        backend.post_message.assert_not_called()

    def test_a_registered_outage_alarm_still_posts_once(self) -> None:
        backend = _backend()

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch.object(notify_module, "resolve_user_id", return_value="U0DEMOOWNER"),
        ):
            _deliver_owner_alert(_alarm("outbound_unposted"), None)

        backend.post_message.assert_called_once()
