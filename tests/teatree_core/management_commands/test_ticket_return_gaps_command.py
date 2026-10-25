from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.models import ConsolidatedMemory, Ticket
from teatree.core.models.dream_gap_ledger import pending_entries
from tests.factories import PullRequestFactory, TaskFactory, WorktreeFactory

UMBRELLA = "https://github.com/o/r/issues/249"


class ReturnGapsCommandTests(TestCase):
    def setUp(self) -> None:
        patcher = patch(
            "teatree.core.models.dream_gap_ledger.get_effective_settings",
            return_value=UserSettings(dream_umbrella_url=UMBRELLA),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.umbrella = Ticket.objects.create(issue_url=UMBRELLA, state=Ticket.State.MERGED)

    def member(self, key: str, *, state: str = Ticket.State.WORK_STARTED) -> Ticket:
        return Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap={key}",
            state=state,
            extra={"dream_gap_batch": [{"gap_key": key, "cluster_key": key, "detail": "Original"}]},
        )

    def returned(
        self, *members: Ticket, confirm: str = "", advanced: str = "", stderr: StringIO | None = None
    ) -> object:
        ids = ",".join(str(member.pk) for member in members)
        args = ["ticket", "return-gaps", "--ids", ids]
        if confirm:
            args.extend(["--confirm", confirm])
        if advanced:
            args.extend(["--advanced", advanced])
        return call_command(*args, **({"stderr": stderr} if stderr is not None else {}))

    def refused(self, *members: Ticket, reason: str, confirm: str = "", advanced: str = "") -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit) as error:
            self.returned(*members, confirm=confirm, advanced=advanced, stderr=stderr)
        assert error.value.code == 1
        assert reason in stderr.getvalue()

    def test_returns_all_keys_and_keeps_memory_row(self) -> None:
        member = self.member("g1")
        member.extra["dream_gap_batch"].append({"gap_key": "g2", "cluster_key": "g2"})
        member.extra["dream_gap_claimed_delivered"] = ["g1", "g2"]
        member.save(update_fields=["extra"])
        memory = ConsolidatedMemory.objects.create(
            cluster_key="g1",
            rule="Keep this rule",
            source_files=["feedback.md"],
            is_binding=False,
            member_count=1,
            max_member_weight=90,
        )

        self.returned(member)

        member.refresh_from_db()
        self.umbrella.refresh_from_db()
        memory.refresh_from_db()
        assert member.state == Ticket.State.IGNORED
        assert member.extra["dream_gap_claimed_delivered"] == []
        assert [entry["gap_key"] for entry in pending_entries(self.umbrella)] == ["g1", "g2"]
        assert "returned from ticket" in pending_entries(self.umbrella)[0]["detail"].lower()
        assert memory.rule == "Keep this rule"
        self.returned(member)
        self.umbrella.refresh_from_db()
        assert [entry["gap_key"] for entry in pending_entries(self.umbrella)] == ["g1", "g2"]

    def test_release_stamp_can_restore_batch_and_claims(self) -> None:
        member = self.member("g1")
        member.extra["dream_gap_batch"][0]["offered_at"] = "2026-10-01T00:00:00+00:00"
        member.extra["dream_gap_claimed_delivered"] = ["g1"]
        member.save(update_fields=["extra"])
        original_batch = list(member.extra["dream_gap_batch"])

        self.returned(member)

        member.refresh_from_db()
        stamp = member.extra["dream_gap_released"]
        assert member.extra["dream_gap_batch"] == []
        assert member.extra["dream_gap_claimed_delivered"] == []
        assert stamp["batch"] == original_batch
        assert stamp["claimed_delivered"] == ["g1"]
        self.umbrella.refresh_from_db()
        assert "offered_at" not in pending_entries(self.umbrella)[0]
        member.merge_extra(
            set_keys={"dream_gap_batch": stamp["batch"], "dream_gap_claimed_delivered": stamp["claimed_delivered"]}
        )
        member.refresh_from_db()
        assert member.extra["dream_gap_batch"] == original_batch
        assert member.extra["dream_gap_claimed_delivered"] == ["g1"]

    def test_pr_opened_state_without_a_pr_row_is_held(self) -> None:
        member = self.member("g1", state=Ticket.State.PR_OPENED)

        self.refused(member, reason="has opened a pull request", advanced=str(member.pk))

        member.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert member.state == Ticket.State.PR_OPENED
        assert pending_entries(self.umbrella) == []

    def test_advanced_return_holds_every_post_open_state_without_a_pr_row(self) -> None:
        states = (
            Ticket.State.REVIEW_REQUESTED,
            Ticket.State.MERGED,
            Ticket.State.RETRO_RECORDED,
            Ticket.State.DELIVERED,
            Ticket.State.REVIEW_DELIVERED,
        )
        for number, state in enumerate(states):
            with self.subTest(state=state):
                member = self.member(f"g{number}", state=state)
                self.refused(member, reason="has opened a pull request", advanced=str(member.pk))

    def test_refuses_claim_for_a_key_outside_the_batch(self) -> None:
        member = self.member("g1")
        member.extra["dream_gap_claimed_delivered"] = ["g1", "foreign"]
        member.save(update_fields=["extra"])

        self.refused(member, reason="claims keys outside its gap batch")

        member.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert member.state == Ticket.State.WORK_STARTED
        assert member.extra["dream_gap_claimed_delivered"] == ["g1", "foreign"]
        assert pending_entries(self.umbrella) == []

    def test_refuses_live_task_pr_and_worktree(self) -> None:
        active = self.member("task")
        TaskFactory(ticket=active)
        pr = self.member("pr")
        PullRequestFactory(ticket=pr)
        with TemporaryDirectory() as directory:
            worktree = self.member("worktree")
            WorktreeFactory(ticket=worktree, extra={"worktree_path": directory})
            for member, reason in (
                (active, "has a live task"),
                (pr, "has a pull request"),
                (worktree, "has an existing worktree"),
            ):
                with self.subTest(member=member.pk):
                    self.refused(member, reason=reason)
                member.refresh_from_db()
                self.umbrella.refresh_from_db()
                assert member.state == Ticket.State.WORK_STARTED
                assert pending_entries(self.umbrella) == []

    def test_refuses_disposition_and_plan_without_advanced(self) -> None:
        disposition = self.member("done")
        disposition.extra["dream_gap_dispositions"] = {"done": {"disposition": "address", "evidence": "x"}}
        disposition.save(update_fields=["extra"])
        planned = self.member("planned", state=Ticket.State.PLAN_RECORDED)
        for member, reason in ((disposition, "has recorded gap dispositions"), (planned, "needs --advanced")):
            with self.subTest(member=member.pk):
                self.refused(member, reason=reason)
                member.refresh_from_db()
                self.umbrella.refresh_from_db()
                assert member.state != Ticket.State.IGNORED
                assert pending_entries(self.umbrella) == []
        self.returned(planned, advanced=str(planned.pk))
        planned.refresh_from_db()
        assert planned.state == Ticket.State.IGNORED

    def test_bulk_confirm_is_per_member_and_atomic(self) -> None:
        members = [self.member(f"g{number}") for number in range(3)]
        with patch(
            "teatree.core.gates.bulk_close_gate.get_effective_settings",
            return_value=UserSettings(bulk_close_threshold=2),
        ):
            self.refused(*members, reason="explicit per-item confirmation")
            self.umbrella.refresh_from_db()
            assert pending_entries(self.umbrella) == []
            self.returned(*members, confirm=",".join(str(member.pk) for member in members))
        self.umbrella.refresh_from_db()
        assert len(pending_entries(self.umbrella)) == 3

    def test_later_refusal_rolls_back_earlier_member(self) -> None:
        safe = self.member("safe")
        blocked = self.member("blocked")
        TaskFactory(ticket=blocked)

        self.refused(safe, blocked, reason="has a live task")

        safe.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert safe.state == Ticket.State.WORK_STARTED
        assert pending_entries(self.umbrella) == []

    def test_stale_worktree_row_does_not_block_return(self) -> None:
        member = self.member("stale")
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "gone"
            WorktreeFactory(ticket=member, extra={"worktree_path": str(missing)})

        self.returned(member)

        member.refresh_from_db()
        assert member.state == Ticket.State.IGNORED

    def test_malformed_member_refuses_without_changes(self) -> None:
        member = Ticket.objects.create(
            issue_url=f"{UMBRELLA}#not-a-member",
            state=Ticket.State.WORK_STARTED,
            extra={"dream_gap_batch": [{"gap_key": "g1", "cluster_key": "g1"}]},
        )
        self.refused(member, reason="is not anchored on a dream-gap member")
        member.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert member.state == Ticket.State.WORK_STARTED
        assert pending_entries(self.umbrella) == []

    def test_missing_pending_write_refuses_without_ignoring_member(self) -> None:
        member = self.member("g1")
        with patch("teatree.core.models.dream_gap_ledger.queue_pending"):
            self.refused(member, reason="gaps were not recorded on the umbrella")
        member.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert member.state == Ticket.State.WORK_STARTED
        assert pending_entries(self.umbrella) == []

    def test_advanced_ids_must_be_numeric_and_in_target_ids(self) -> None:
        member = self.member("g1")
        for advanced, reason in (
            (f"{member.pk},abc", "--advanced needs numeric"),
            ("99999", "--advanced ids must appear in --ids"),
        ):
            with self.subTest(advanced=advanced):
                self.refused(member, advanced=advanced, reason=reason)
                member.refresh_from_db()
                self.umbrella.refresh_from_db()
                assert member.state == Ticket.State.WORK_STARTED
                assert pending_entries(self.umbrella) == []
