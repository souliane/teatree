"""Tests for the ``loop_dispatch`` management command (pending-spawn / spawn-claim)."""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.ticket import schedule_external_review


class _LoopDispatchTest(TestCase):
    def _reviewer_task(self, *, url: str = "https://example.com/pr/1", head_sha: str = "x") -> Task:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head_sha},
        )
        return schedule_external_review(ticket)

    def _author_task(self, *, url: str = "https://example.com/issues/9") -> Task:
        ticket = Ticket.objects.create(overlay="acme", issue_url=url, role=Ticket.Role.AUTHOR)
        return ticket.schedule_coding()


class TestPendingSpawn(_LoopDispatchTest):
    def test_emits_reviewer_subagent_for_reviewer_role(self) -> None:
        task = self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert len(payload) == 1
        entry = payload[0]
        assert entry["task_id"] == task.pk
        assert entry["subagent"] == "t3:reviewer"
        assert entry["phase"] == "reviewing"
        assert entry["ticket_role"] == Ticket.Role.REVIEWER
        assert entry["issue_url"] == "https://example.com/pr/1"

    def test_emits_coder_subagent_for_author_coding(self) -> None:
        task = self._author_task()
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert len(payload) == 1
        assert payload[0]["task_id"] == task.pk
        assert payload[0]["subagent"] == "t3:coder"

    def test_skips_claimed_tasks(self) -> None:
        task = self._reviewer_task()
        task.claim(claimed_by="loop-slot")
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        assert json.loads(stdout.getvalue()) == []

    def test_skips_tasks_with_no_registered_subagent(self) -> None:
        # A scoping task on an author ticket → no _SUBAGENT_BY_PHASE entry → skipped.
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/77")
        session = Session.objects.create(ticket=ticket, agent_id="scoping")
        Task.objects.create(ticket=ticket, session=session, phase="scoping")
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        assert json.loads(stdout.getvalue()) == []

    def test_text_output_when_empty(self) -> None:
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", stdout=stdout)
        assert "No pending spawn requests." in stdout.getvalue()

    def test_payload_carries_model_and_skill_bundle(self) -> None:
        # The model tier + skill bundle are resolved in LOOP scope and threaded
        # into the dispatch payload so the in-session /loop slot passes them to
        # its Agent (not inside a claude -p subprocess).
        self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        entry = json.loads(stdout.getvalue())[0]
        assert "model" in entry
        # reviewing is a mechanical phase → sonnet tier by default.
        assert entry["model"] == "sonnet"
        assert isinstance(entry["skill_bundle"], list)

    def test_payload_never_carries_an_effort_key(self) -> None:
        # Effort is session-wide only — the per-sub-agent dispatch payload (which
        # feeds the Agent tool, which has no effort param) must NEVER carry it.
        self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)
        entry = json.loads(stdout.getvalue())[0]
        assert "effort" not in entry
        assert "session_effort" not in entry

    def test_skill_floor_raises_the_dispatch_model(self) -> None:
        # A per-skill MODEL floor on a skill in the resolved bundle raises the
        # dispatch payload's model above the phase tier (most-capable-wins).
        cfg = Path(tempfile.mkdtemp()) / ".teatree.toml"
        cfg.write_text('[agent.skill_models]\ncode-review = "fable"\n', encoding="utf-8")

        self._reviewer_task()
        stdout = StringIO()
        with (
            patch("teatree.agents.model_tiering.CONFIG_PATH", cfg),
            patch("teatree.config_agent.CONFIG_PATH", cfg),
            patch("teatree.agents.skill_bundle.resolve_skill_bundle", return_value=["code-review"]),
        ):
            call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)
        entry = json.loads(stdout.getvalue())[0]
        # reviewing's sonnet phase floor raised to fable by the code-review skill.
        assert entry["model"] == "fable"
        assert entry["skill_bundle"] == ["code-review"]


class TestClaimNextAtomicDispatch(_LoopDispatchTest):
    """#786 N4 keystone: claim-then-spawn so two ticks never double-dispatch one Task.

    The claim boundary IS the spawn boundary. The pre-fix flow
    (``pending-spawn`` lists ALL unclaimed → Agent → ``spawn-claim``
    after) let two ticks both see the same Task and both spawn before
    either claimed. ``claim-next`` claims atomically and only then emits
    the dispatch payload for the just-claimed Task.
    """

    def test_claim_next_claims_then_emits_one_task(self) -> None:
        task = self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert len(payload) == 1
        assert payload[0]["task_id"] == task.pk
        assert payload[0]["subagent"] == "t3:reviewer"
        # Claimed BEFORE the payload was emitted (claim == spawn boundary).
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "loop-slot"

    def test_two_sequential_ticks_never_double_dispatch_same_task(self) -> None:
        """THE N4 KEYSTONE: one pending Task, two ticks, dispatched exactly once.

        Exactly one tick gets it, the other gets nothing — never the
        same Task twice.
        """
        task = self._reviewer_task()

        out1, out2 = StringIO(), StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=out1)
        call_command("loop_dispatch", "claim-next", "--json", stdout=out2)

        first = json.loads(out1.getvalue())
        second = json.loads(out2.getvalue())
        dispatched_ids = [e["task_id"] for e in first] + [e["task_id"] for e in second]
        # The single Task is dispatched exactly once across the two ticks.
        assert dispatched_ids.count(task.pk) == 1
        assert second == []  # second tick found nothing claimable
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    def test_two_ticks_two_tasks_each_gets_a_distinct_task(self) -> None:
        t_a = self._reviewer_task(url="https://example.com/pr/1", head_sha="a")
        t_b = self._reviewer_task(url="https://example.com/pr/2", head_sha="b")

        out1, out2 = StringIO(), StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=out1)
        call_command("loop_dispatch", "claim-next", "--json", stdout=out2)

        got = sorted(
            [e["task_id"] for e in json.loads(out1.getvalue())] + [e["task_id"] for e in json.loads(out2.getvalue())],
        )
        assert got == sorted([t_a.pk, t_b.pk])  # both dispatched, no overlap

    def test_claim_next_empty_when_nothing_pending(self) -> None:
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
        assert json.loads(stdout.getvalue()) == []

    def test_claim_next_skips_tasks_with_no_registered_subagent(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/77")
        session = Session.objects.create(ticket=ticket, agent_id="scoping")
        Task.objects.create(ticket=ticket, session=session, phase="scoping")
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
        assert json.loads(stdout.getvalue()) == []

    def test_claim_next_text_output_when_claimed(self) -> None:
        """N3: the non-JSON branch — emits a human line for the claimed task."""
        task = self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", stdout=stdout)

        out = stdout.getvalue()
        assert f"Claimed task={task.pk}" in out
        assert "subagent=t3:reviewer" in out
        assert "phase=reviewing" in out
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    def test_claim_next_text_output_when_empty(self) -> None:
        """N3: the non-JSON empty branch."""
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", stdout=stdout)
        assert "No pending spawn requests." in stdout.getvalue()


class TestSpawnClaim(_LoopDispatchTest):
    def test_claims_pending_task(self) -> None:
        task = self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "spawn-claim", str(task.pk), stdout=stdout)

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "loop-slot"
        assert "Claimed task" in stdout.getvalue()

    def test_unknown_task_errors(self) -> None:
        with pytest.raises(SystemExit):
            call_command("loop_dispatch", "spawn-claim", "999999")

    def test_claim_with_custom_worker(self) -> None:
        task = self._reviewer_task()
        call_command("loop_dispatch", "spawn-claim", str(task.pk), claimed_by="custom-worker")
        task.refresh_from_db()
        assert task.claimed_by == "custom-worker"
