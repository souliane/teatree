"""Tests for the ``loop_dispatch`` management command (pending-spawn / spawn-claim)."""

import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.agents.model_tiering import TIER_MODELS
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
        assert entry["model"] == TIER_MODELS["frontier"]
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

    def test_claim_next_reclaims_a_dead_sessions_orphaned_unit(self) -> None:
        """Defect (b): the standalone ``claim-next`` reclaims a dead session's stale lease.

        Session A claims the unit, then dies — its lease lapses. The next
        healthy session's ``claim-next`` reclaims and dispatches that SAME unit
        exactly once. Previously only the full loop tick's recovery sweep
        (``_reap_stale_task_claims``) returned an orphan to PENDING, so on the
        standalone self-pump / slack-answer path a dead session's unit stalled
        CLAIMED forever and the loop silently stopped picking it up.

        Anti-vacuity: on the pre-fix command (no reclaim before the claim) the
        unit is still CLAIMED — not PENDING — so ``claim_next_pending`` returns
        nothing and the payload is empty. The ``len(payload) == 1`` assertion
        is RED on the buggy code, GREEN once the reclaim runs first.
        """
        task = self._reviewer_task()
        task.claim(claimed_by="loop-slot")
        # Session A dies: force its lease into the past (no more heartbeats).
        task.lease_expires_at = timezone.now() - timedelta(seconds=10)
        task.save(update_fields=["lease_expires_at"])

        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert len(payload) == 1
        assert payload[0]["task_id"] == task.pk
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > timezone.now()  # a fresh lease for the reclaiming session

    def test_claim_next_does_not_reclaim_a_live_lease(self) -> None:
        """Live-lease protection at the command level: a FRESH lease is never stolen.

        The reclaim is staleness-gated — a unit whose owner still holds a live
        lease is left untouched, so ``claim-next`` dispatches nothing and the
        living owner keeps its claim. Guards the reclaim against turning into a
        blanket steal of in-flight work.
        """
        task = self._reviewer_task()
        task.claim(claimed_by="loop-slot")  # a fresh 300s lease

        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)

        assert json.loads(stdout.getvalue()) == []  # the live owner keeps the unit
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "loop-slot"

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

    def test_claim_next_session_defaults_to_current_session_id(self) -> None:
        """#1917: an unset ``--claimed-by-session`` resolves to the active session id."""
        task = self._reviewer_task()
        stdout = StringIO()
        with patch("teatree.core.session_identity.current_session_id", return_value="sess-default"):
            call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload[0]["claimed_by_session"] == "sess-default"
        task.refresh_from_db()
        assert task.claimed_by_session == "sess-default"

    def test_claim_next_explicit_session_overrides_default(self) -> None:
        """#1917: an explicit ``--claimed-by-session`` is threaded through and surfaced."""
        task = self._reviewer_task()
        stdout = StringIO()
        with patch("teatree.core.session_identity.current_session_id", return_value="should-not-be-used"):
            call_command("loop_dispatch", "claim-next", "--json", claimed_by_session="sess-explicit", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload[0]["claimed_by_session"] == "sess-explicit"
        task.refresh_from_db()
        assert task.claimed_by_session == "sess-explicit"

    def test_claim_next_empty_session_surfaced_when_unresolvable(self) -> None:
        """#1917 inert: when no session resolves, the claim carries an empty session."""
        task = self._reviewer_task()
        stdout = StringIO()
        with patch("teatree.core.session_identity.current_session_id", return_value=""):
            call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload[0]["claimed_by_session"] == ""
        task.refresh_from_db()
        assert task.claimed_by_session == ""


class TestClaimNextAdmitBudgetGate(_LoopDispatchTest):
    """#1796 (WI-1): ``claim-next`` honours the orchestrate admit-budget ceiling.

    The reconciled fan-out persists a per-tick admit budget to the tick-meta
    sidecar (read-only PLANNER); the live claimer reads it before its CAS and
    refuses once the standing in-flight CLAIMED WIP hits the ceiling, so
    claimed ≡ spawned and the orphan window is closed.

    Absence of a budget (medium / toggle-off) is UNCLAMPED — today's
    throughput, byte-identical. A stale budget (> TTL) is ignored, also
    unclamped, so a dead loop never wrongly throttles live dispatch.
    """

    def _claim_in_flight(self, n: int) -> list[Task]:
        """Seed *n* dispatchable tasks as CLAIMED with a live lease (in flight)."""
        claimed: list[Task] = []
        for i in range(n):
            task = self._author_task(url=f"https://example.com/issues/inflight/{i}")
            task.claim(claimed_by="other-worker")
            claimed.append(task)
        return claimed

    def _run_claim_next(self, sl: Path) -> list[dict]:
        stdout = StringIO()
        with patch("teatree.core.management.commands.loop_dispatch.default_path", return_value=sl):
            call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
        return json.loads(stdout.getvalue())

    def test_no_budget_key_drains_all_pending_unclamped(self) -> None:
        # medium / toggle-off → no budget written → unclamped (today's behaviour).
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._author_task(url="https://example.com/issues/a")
            payload = self._run_claim_next(sl)
        assert len(payload) == 1  # claimed despite no budget key

    def test_full_with_budget_admits_exactly_budget_then_refuses(self) -> None:
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            for i in range(3):
                self._author_task(url=f"https://example.com/issues/q/{i}")
            write_admit_budget(2, statusline_path=sl)
            first = self._run_claim_next(sl)
            second = self._run_claim_next(sl)
            third = self._run_claim_next(sl)
        # Budget 2: two claims land, the third is refused (in-flight 2 >= 2).
        assert len(first) == 1
        assert len(second) == 1
        assert third == []
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 2
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1

    def test_in_flight_at_budget_refuses_the_next_claim(self) -> None:
        # THE anti-vacuous core: B already in flight + budget B → claim ZERO.
        # RED on the pre-fix code (no clamp → it would claim the pending row).
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._claim_in_flight(2)
            self._author_task(url="https://example.com/issues/pending")
            write_admit_budget(2, statusline_path=sl)
            payload = self._run_claim_next(sl)
        assert payload == []  # the gate, not the CAS, holds the row
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1

    def test_freeing_one_lease_lets_exactly_one_more_claim(self) -> None:
        # Prove the gate is the ONLY thing holding the row: clear one in-flight
        # lease (reclaim it to PENDING) and the next claim takes exactly one.
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            in_flight = self._claim_in_flight(2)
            self._author_task(url="https://example.com/issues/pending")
            write_admit_budget(2, statusline_path=sl)
            blocked = self._run_claim_next(sl)
            assert blocked == []

            # Expire one in-flight lease and reclaim it → in-flight drops to 1.
            in_flight[0].lease_expires_at = timezone.now() - timedelta(seconds=10)
            in_flight[0].save(update_fields=["lease_expires_at"])
            Task.objects.reclaim_orphaned_claims()

            after = self._run_claim_next(sl)
            again = self._run_claim_next(sl)
        # Exactly one more claim lands (in-flight 1 < budget 2), then it refuses.
        assert len(after) == 1
        assert again == []

    def test_stale_budget_past_ttl_is_ignored_unclamped(self) -> None:
        # A budget written long ago (dead loop) is ignored → unclamped drain.
        import json as _json  # noqa: PLC0415
        import time as _time  # noqa: PLC0415

        from teatree.loop.admit_budget import BUDGET_KEY, WRITTEN_AT_KEY  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            meta = sl.with_name("tick-meta.json")
            stale_at = _time.time() - (2 * 720 + 600)
            meta.write_text(
                _json.dumps({BUDGET_KEY: 0, WRITTEN_AT_KEY: stale_at}) + "\n",
                encoding="utf-8",
            )
            self._claim_in_flight(1)
            self._author_task(url="https://example.com/issues/pending")
            payload = self._run_claim_next(sl)
        # Budget 0 would refuse — but it is stale, so ignored → the row claims.
        assert len(payload) == 1

    def test_budget_read_error_fails_open_unclamped(self) -> None:
        # A budget-read failure must NEVER clamp — the gate fails open so a
        # broken sidecar read can never starve live dispatch.
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._author_task(url="https://example.com/issues/a")
            stdout = StringIO()
            with (
                patch("teatree.core.management.commands.loop_dispatch.default_path", return_value=sl),
                patch(
                    "teatree.core.management.commands.loop_dispatch.read_admit_budget",
                    side_effect=RuntimeError("sidecar exploded"),
                ),
            ):
                call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
            assert len(json.loads(stdout.getvalue())) == 1  # claimed despite the error

    def test_no_claimed_but_unspawned_rows_after_a_budgeted_wave(self) -> None:
        # Reconciliation invariant: with the gate armed, every CLAIMED row is one
        # the caller will spawn — there is no claimed-but-orphaned surplus. We
        # claim a full budgeted wave and assert claimed == budget exactly.
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            for i in range(5):
                self._author_task(url=f"https://example.com/issues/wave/{i}")
            write_admit_budget(3, statusline_path=sl)
            for _ in range(5):  # five attempts, only three may claim
                self._run_claim_next(sl)
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 3
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 2


class TestPendingSpawnClaimableOnly(_LoopDispatchTest):
    """``pending-spawn --claimable-only`` mirrors what ``claim-next`` would take.

    TODO #100: the Stop-hook self-pump probes ``pending-spawn`` to decide
    "is there work to continue the loop?". The legacy probe reports EVERY
    dispatchable PENDING task regardless of the admit budget, but
    ``claim-next`` refuses once the in-flight CLAIMED WIP reaches the
    ceiling. So when the budget is exhausted, the probe reports the same
    PENDING unit forever while ``claim-next`` claims nothing — the
    self-pump re-offers a unit it can never advance. ``--claimable-only``
    applies the same admit-budget gate the claimer applies, so the probe
    reports work ONLY when a claim could actually land.
    """

    def _claim_in_flight(self, n: int) -> list[Task]:
        claimed: list[Task] = []
        for i in range(n):
            task = self._author_task(url=f"https://example.com/issues/inflight/{i}")
            task.claim(claimed_by="other-worker")
            claimed.append(task)
        return claimed

    def _run_pending_claimable(self, sl: Path) -> list[dict]:
        stdout = StringIO()
        with patch("teatree.core.management.commands.loop_dispatch.default_path", return_value=sl):
            call_command("loop_dispatch", "pending-spawn", "--json", "--claimable-only", stdout=stdout)
        return json.loads(stdout.getvalue())

    def test_budget_exhausted_reports_no_claimable_work(self) -> None:
        # THE anti-vacuous core (RED on the pre-fix code): budget B, B already
        # in flight, one more PENDING → claim-next would refuse → the
        # claimable-only probe must report ZERO so the self-pump stops
        # re-offering the un-advanceable unit.
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._claim_in_flight(2)
            self._author_task(url="https://example.com/issues/pending")
            write_admit_budget(2, statusline_path=sl)
            payload = self._run_pending_claimable(sl)
        assert payload == []
        # The legacy probe (no gate) still reports the un-advanceable unit —
        # proving the gate is what changed the answer, not the data.
        legacy = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=legacy)
        assert len(json.loads(legacy.getvalue())) == 1

    def test_under_budget_reports_the_claimable_unit(self) -> None:
        # Control: in-flight 1 < budget 2 → a claim could land → the probe
        # reports the PENDING unit (it did not just blanket-suppress).
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._claim_in_flight(1)
            self._author_task(url="https://example.com/issues/pending")
            write_admit_budget(2, statusline_path=sl)
            payload = self._run_pending_claimable(sl)
        assert len(payload) == 1
        assert payload[0]["issue_url"] == "https://example.com/issues/pending"

    def test_no_budget_key_reports_all_dispatchable_unclamped(self) -> None:
        # Absence of a budget (medium / toggle-off) is UNCLAMPED — the probe
        # reports the pending unit exactly as the legacy probe does today.
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._author_task(url="https://example.com/issues/a")
            payload = self._run_pending_claimable(sl)
        assert len(payload) == 1

    def test_budget_read_error_fails_open_unclamped(self) -> None:
        # A budget-read failure must NEVER clamp the probe — fail open so a
        # broken sidecar read can never wedge the self-pump into idle.
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._author_task(url="https://example.com/issues/a")
            stdout = StringIO()
            with (
                patch("teatree.core.management.commands.loop_dispatch.default_path", return_value=sl),
                patch(
                    "teatree.core.management.commands.loop_dispatch.read_admit_budget",
                    side_effect=RuntimeError("sidecar exploded"),
                ),
            ):
                call_command("loop_dispatch", "pending-spawn", "--json", "--claimable-only", stdout=stdout)
            assert len(json.loads(stdout.getvalue())) == 1

    def test_default_probe_is_unchanged_no_gate(self) -> None:
        # Without --claimable-only the legacy probe is byte-identical: it
        # reports the un-advanceable unit even at a full budget (the legacy
        # callers must not change behaviour).
        from teatree.loop.admit_budget import write_admit_budget  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            self._claim_in_flight(2)
            self._author_task(url="https://example.com/issues/pending")
            write_admit_budget(2, statusline_path=sl)
            stdout = StringIO()
            with patch("teatree.core.management.commands.loop_dispatch.default_path", return_value=sl):
                call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)
        assert len(json.loads(stdout.getvalue())) == 1


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
