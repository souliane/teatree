from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.modelkit.dm_channel_policy import DmChannel, classify
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing, MergeAudit, MergeClear
from teatree.core.notify import NotifyReason
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.unaudited_merge_alert import record_unaudited_merges

SHA = "b" * 40


def _signal(sha: str = SHA) -> ScanSignal:
    return ScanSignal(
        kind="merge.unaudited",
        summary="ALERT",
        payload={
            "slug": "acme/widget",
            "pr_id": 17,
            "url": "https://github.com/acme/widget/pull/17",
            "merged_sha": sha,
            "merged_by": "operator",
            "merged_at": "2026-10-25T12:00:00Z",
            "independent_review_at_head": True,
            "hold_at_head": True,
        },
    )


class TestUnauditedMergeAlert(TestCase):
    def test_the_alert_carries_the_required_facts_on_a_push_key(self) -> None:
        calls: list[tuple[str, str]] = []

        def notify(text: str, *, kind: str, idempotency_key: str, audience: NotifyAudience) -> None:
            assert audience is NotifyAudience.OWNER_ESCALATION
            calls.append((text, idempotency_key))

        record_unaudited_merges([_signal()], notify=notify)

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

    def test_a_merge_audited_after_the_scan_is_not_alerted(self) -> None:
        """Mutant: drop the alert-time recheck and a merge audited between scan and alert still pages the owner."""
        clear = MergeClear.objects.create(pr_id=17, slug="acme/widget", reviewed_sha="a" * 40)
        MergeAudit.objects.create(clear=clear, merged_sha=SHA.upper(), repo_slug="acme/widget")
        notify = MagicMock()

        record_unaudited_merges([_signal()], notify=notify)

        notify.assert_not_called()

    def test_the_real_notifier_leaves_one_ping_per_merge_across_ticks(self) -> None:
        """Mutants: a key without the sha merges two merges of one PR; a key with the clock pings each tick."""
        backend = MagicMock()
        backend.open_dm.return_value = "D-OWNER"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
        backend.get_permalink.return_value = "https://acme.slack.com/archives/D-OWNER/p1700000000000100"
        backend.fetch_message.return_value = {"ts": "1700000000.000100", "text": "alert"}
        with (
            patch("teatree.core.notify.resolve_owner_dm_backend", return_value=(backend, NotifyReason.NONE)),
            patch("teatree.core.notify.resolve_user_id", return_value="U_OWNER"),
        ):
            record_unaudited_merges([_signal()])
            record_unaudited_merges([_signal()])
            record_unaudited_merges([_signal("c" * 40)])

        keys = BotPing.objects.filter(idempotency_key__startswith="unaudited-merge:").values_list(
            "idempotency_key", flat=True
        )
        assert sorted(keys) == [
            f"unaudited-merge:acme/widget#17:{'b' * 12}",
            f"unaudited-merge:acme/widget#17:{'c' * 12}",
        ]
