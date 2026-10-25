from django.test import TestCase

from teatree.core.modelkit.dm_channel_policy import DmChannel, classify
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.unaudited_merge_alert import record_unaudited_merges


def _signal() -> ScanSignal:
    return ScanSignal(
        kind="merge.unaudited",
        summary="ALERT",
        payload={
            "slug": "acme/widget",
            "pr_id": 17,
            "url": "https://github.com/acme/widget/pull/17",
            "merged_sha": "b" * 40,
            "merged_by": "operator",
            "merged_at": "2026-10-25T12:00:00Z",
            "independent_review_at_head": True,
            "hold_at_head": True,
        },
    )


class TestUnauditedMergeAlert(TestCase):
    def test_two_ticks_use_one_push_key_and_carry_the_required_facts(self) -> None:
        calls: list[tuple[str, str]] = []

        def notify(text: str, *, kind: str, idempotency_key: str, audience: NotifyAudience) -> None:
            assert audience is NotifyAudience.OWNER_ESCALATION
            calls.append((text, idempotency_key))

        record_unaudited_merges([_signal()], notify=notify)
        record_unaudited_merges([_signal()], notify=notify)

        assert len(calls) == 2
        assert len({key for _, key in calls}) == 1
        text, key = calls[0]
        assert all(
            value in text
            for value in (
                "operator",
                "2026-10-25T12:00:00Z",
                "Independent review at head: yes",
                "Hold at head: yes",
                "https://github.com/acme/widget/pull/17",
                "Confirm",
            )
        )
        assert classify(audience=NotifyAudience.OWNER_ESCALATION, idempotency_key=key) is DmChannel.PUSH
