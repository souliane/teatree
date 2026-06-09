"""Idle-stack reaper + queue-drainer scanners (#2190, #44).

The reaper emits ``local_stack.reap_idle`` per reapable worktree; the drainer
emits ``local_stack.queue_acquire`` per due queue item. Both are global
(``overlay=""``) mechanical scanners — the actual stop/start runs in the
paired mechanical handlers, never an agent.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LocalStackQueueItem, Ticket, Worktree
from teatree.loop.scanners.idle_stack_reaper import IdleStackReaperScanner
from teatree.loop.scanners.local_stack_queue_drainer import LocalStackQueueDrainerScanner


def _worktree(*, overlay: str, ticket_number: str, state: Worktree.State, idle_minutes_ago: int = 60) -> Worktree:
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://example.com/{overlay}/issues/{ticket_number}",
    )
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path="backend",
        branch=f"{ticket_number}-feat",
        state=state,
        last_used_at=timezone.now() - timedelta(minutes=idle_minutes_ago),
    )


class TestIdleStackReaperScanner(TestCase):
    def test_emits_reap_signal_for_idle_running_worktree(self) -> None:
        wt = _worktree(overlay="t3-heavy", ticket_number="600", state=Worktree.State.SERVICES_UP)
        scanner = IdleStackReaperScanner(overlay="t3-heavy", idle_minutes=30)
        with patch(
            "teatree.loop.scanners.idle_stack_reaper.reapable_worktrees",
            return_value=[wt],
        ):
            signals = scanner.scan()
        assert len(signals) == 1
        assert signals[0].kind == "local_stack.reap_idle"
        assert signals[0].payload["worktree_id"] == wt.pk

    def test_emits_nothing_when_no_idle_worktree(self) -> None:
        scanner = IdleStackReaperScanner(overlay="t3-heavy", idle_minutes=30)
        with patch("teatree.loop.scanners.idle_stack_reaper.reapable_worktrees", return_value=[]):
            assert scanner.scan() == []

    def test_name_is_stable(self) -> None:
        assert IdleStackReaperScanner(overlay="t3-heavy", idle_minutes=30).name == "idle_stack_reaper"


class TestLocalStackQueueDrainerScanner(TestCase):
    def test_emits_acquire_signal_for_due_item(self) -> None:
        wt = _worktree(overlay="t3-heavy", ticket_number="700", state=Worktree.State.PROVISIONED)
        item = LocalStackQueueItem.objects.create(overlay="t3-heavy", worktree=wt)
        scanner = LocalStackQueueDrainerScanner(overlay="t3-heavy")
        signals = scanner.scan()
        assert len(signals) == 1
        assert signals[0].kind == "local_stack.queue_acquire"
        assert signals[0].payload["queue_item_id"] == item.pk

    def test_skips_item_not_yet_due(self) -> None:
        wt = _worktree(overlay="t3-heavy", ticket_number="701", state=Worktree.State.PROVISIONED)
        LocalStackQueueItem.objects.create(
            overlay="t3-heavy",
            worktree=wt,
            status=LocalStackQueueItem.Status.RETRYING,
            attempt_count=1,
            next_attempt_at=timezone.now() + timedelta(minutes=5),
        )
        assert LocalStackQueueDrainerScanner(overlay="t3-heavy").scan() == []

    def test_scopes_to_overlay(self) -> None:
        other_wt = _worktree(overlay="t3-other", ticket_number="702", state=Worktree.State.PROVISIONED)
        LocalStackQueueItem.objects.create(overlay="t3-other", worktree=other_wt)
        assert LocalStackQueueDrainerScanner(overlay="t3-heavy").scan() == []

    def test_name_is_stable(self) -> None:
        assert LocalStackQueueDrainerScanner(overlay="t3-heavy").name == "local_stack_queue_drainer"
