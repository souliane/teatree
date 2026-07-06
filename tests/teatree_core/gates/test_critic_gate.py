"""critic_gate (SELFCATCH-5): deterministic blocking teeth + async LLM advisory net.

Proves the rebuilt contract. Deterministic items (done_not_done / spec_not_plan /
completeness) REUSE the sibling gates, fire on absence, and are the ONLY items that
block. LLM items are judged by a recorded CriticVerdict (real judgment), never a
self-declared key — a verdict flagging one is mirrored to a CriticFinding but NEVER
blocks. On mark_delivered the async LLM critic is ENQUEUED when no fresh verdict covers
the head. ADVISORY (flag off) records and reaches DELIVERED; ENFORCING (flag on) blocks
on a deterministic finding and stays RETROSPECTED. The enforcing block's findings
SURVIVE the delivery atomic's rollback (execute_retrospect's after-the-block re-record).
Anti-vacuity: with the critic gate neutralised the flawed delivery advances even under
enforcement. No fixture injects an ``extra['critic']`` key — every producer is real
(PlanArtifact, MergeAudit, the spec_coverage manifest, a recorded CriticVerdict).
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.config import UserSettings
from teatree.core import tasks as core_tasks
from teatree.core.gates import critic_gate
from teatree.core.gates.critic_gate import (
    check_critic,
    record_critic_findings,
    record_returned_critic_verdict,
    run_critic,
)
from teatree.core.modelkit import gate_registry
from teatree.core.models import (
    CriticDispatch,
    CriticFinding,
    CriticGateError,
    CriticVerdict,
    MergeAudit,
    MergeClear,
    PlanArtifact,
    Session,
    Task,
    Ticket,
)
from teatree.core.models.plan_adequacy import all_negated_adequacy
from teatree.core.review import critic_rubric
from teatree.core.review.critic_rubric import CRITIC_RUBRIC, CriticRubricItem, RubricKind
from teatree.core.runners.base import RunnerResult

_FORTY_HEX = "a" * 40
_LLM_SLUGS = ("coherence", "duplication", "deferred", "ignored_input", "unenforced_guarantee")


def _critic_envelope() -> dict:
    return {
        "summary": "critic done",
        "critic_verdict": {
            "grader_identity": "critic-agent-7",
            "items": [{"slug": "coherence", "status": "fail", "citation": "x conflated with y"}],
        },
    }


@contextlib.contextmanager
def _enforcement(*, live: bool) -> Iterator[None]:
    with patch.object(critic_gate, "get_effective_settings", return_value=UserSettings(critic_gate_live=live)):
        yield


def _merge_audit(ticket: Ticket) -> None:
    clear = MergeClear.objects.create(
        ticket=ticket,
        pr_id=42,
        slug="souliane/teatree",
        reviewed_sha=_FORTY_HEX,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    MergeAudit.objects.create(clear=clear, merged_sha=_FORTY_HEX, required_checks_status="green")


def _clean_delivered_ticket() -> Ticket:
    """A RETROSPECTED ticket clean on all 3 deterministic items: adequate plan + merge audit + covered ACs."""
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
    PlanArtifact.objects.create(
        ticket=ticket,
        plan_text="plan body",
        recorded_by="planner",
        base_sha=_FORTY_HEX,
        adequacy=dict(all_negated_adequacy("clean delivery")),
    )
    _merge_audit(ticket)
    ticket.extra = {"spec_coverage": {"acceptance_criteria": [{"id": "AC-1", "tests": ["tests/test_a.py::t"]}]}}
    ticket.save(update_fields=["extra"])
    return ticket


def _strip_merge_evidence(ticket: Ticket) -> None:
    MergeAudit.objects.filter(clear__ticket=ticket).delete()
    MergeClear.objects.filter(ticket=ticket).delete()


def _record_critic(ticket: Ticket) -> None:
    """Run the critic and persist its findings (the advisory recording path)."""
    record_critic_findings(ticket, run_critic(ticket))


def _record_verdict(ticket: Ticket, *, slug: str, status: str, citation: str = "cite x.py:1") -> None:
    CriticVerdict.record_from_envelope(
        ticket=ticket,
        transition="mark_delivered",
        head_sha=_FORTY_HEX,
        envelope={
            "grader_identity": "critic-agent-7",
            "items": [{"slug": slug, "status": status, "citation": citation}],
        },
    )


class TestDeterministicItemsCaught(TestCase):
    """Each deterministic item, exhibited alone, produces a finding via the critic; the twin does not."""

    def test_spec_not_plan(self) -> None:
        ticket = _clean_delivered_ticket()
        PlanArtifact.objects.filter(ticket=ticket).delete()
        _record_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="spec_not_plan").exists()

    def test_done_not_done(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        _record_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_completeness(self) -> None:
        ticket = _clean_delivered_ticket()
        ticket.extra = {"spec_coverage": {"acceptance_criteria": [{"id": "AC-2", "tests": []}]}}
        ticket.save(update_fields=["extra"])
        _record_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="completeness").exists()

    def test_clean_twin_records_nothing(self) -> None:
        ticket = _clean_delivered_ticket()
        _record_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket).count() == 0


class TestLlmItemsCaught(TestCase):
    """Anti-vacuity for the semantic half: a recorded verdict flagging an LLM item is caught; the twin is not."""

    def test_each_flagged_llm_item_is_mirrored_and_the_clean_twin_is_not(self) -> None:
        for slug in _LLM_SLUGS:
            with self.subTest(slug=slug):
                caught = _clean_delivered_ticket()
                _record_verdict(caught, slug=slug, status="fail")
                _record_critic(caught)
                assert CriticFinding.objects.filter(ticket=caught, rubric_item=slug).exists(), f"{slug} not caught"

                twin = _clean_delivered_ticket()
                _record_verdict(twin, slug=slug, status="pass", citation="inspected, clean")
                _record_critic(twin)
                assert not CriticFinding.objects.filter(ticket=twin, rubric_item=slug).exists(), (
                    f"{slug} false positive"
                )


class TestEnqueue(TestCase):
    def test_mark_delivered_enqueues_the_async_critic_when_live_and_no_verdict(self) -> None:
        ticket = _clean_delivered_ticket()
        with _enforcement(live=True):
            check_critic(ticket)
        dispatch = CriticDispatch.objects.filter(ticket=ticket, transition="mark_delivered").first()
        assert dispatch is not None
        assert dispatch.task is not None
        assert dispatch.task.phase == "critic_reviewing"  # its OWN phase, not "reviewing"

    def test_dark_flag_off_is_cost_inert_no_dispatch_created(self) -> None:
        # The MED cost-leak fix: while the flag is DARK, the EXPENSIVE async LLM critic
        # must not be armed — no Session / Task / CriticDispatch created on mark_delivered.
        ticket = _clean_delivered_ticket()
        sessions_before = Session.objects.count()
        tasks_before = Task.objects.count()
        with _enforcement(live=False):
            check_critic(ticket)
        assert not CriticDispatch.objects.filter(ticket=ticket).exists()
        assert Session.objects.count() == sessions_before
        assert Task.objects.count() == tasks_before

    def test_no_re_enqueue_when_a_verdict_already_covers_the_head(self) -> None:
        ticket = _clean_delivered_ticket()
        _record_verdict(ticket, slug="coherence", status="pass", citation="clean")
        with _enforcement(live=True):
            check_critic(ticket)
        assert not CriticDispatch.objects.filter(ticket=ticket).exists()


class TestEnforcementGating(TestCase):
    def test_advisory_records_but_does_not_raise(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        with _enforcement(live=False):
            check_critic(ticket)  # no raise
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_enforcing_blocks_on_a_deterministic_finding(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        with _enforcement(live=True), pytest.raises(CriticGateError) as exc:
            check_critic(ticket)
        assert "done_not_done" in str(exc.value)
        assert "critic_gate_live false" in str(exc.value)

    def test_enforcing_never_blocks_on_an_llm_finding(self) -> None:
        # A flagged LLM item is advisory — it records a finding but must NOT block delivery.
        ticket = _clean_delivered_ticket()
        _record_verdict(ticket, slug="coherence", status="fail")
        with _enforcement(live=True):
            check_critic(ticket)  # no raise — LLM items never block
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="coherence").exists()

    def test_enforcing_passes_a_clean_delivery(self) -> None:
        ticket = _clean_delivered_ticket()
        with _enforcement(live=True):
            check_critic(ticket)  # no raise


class TestReturnedVerdictRecording(TestCase):
    def test_records_the_verdict_and_mirrors_findings(self) -> None:
        ticket = _clean_delivered_ticket()
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="c")
        assert dispatch is not None
        envelope = {
            "critic_verdict": {
                "grader_identity": "critic-agent-7",
                "items": [{"slug": "coherence", "status": "fail", "citation": "x conflated with y"}],
            }
        }
        error = record_returned_critic_verdict(dispatch.task, envelope)
        assert error == ""
        assert CriticVerdict.objects.filter(ticket=ticket).exists()
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="coherence").exists()

    def test_refuses_a_maker_graded_verdict(self) -> None:
        ticket = _clean_delivered_ticket()
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="c")
        assert dispatch is not None
        envelope = {"critic_verdict": {"grader_identity": "merge-loop", "items": []}}
        error = record_returned_critic_verdict(dispatch.task, envelope)
        assert "refused" in error
        assert not CriticVerdict.objects.filter(ticket=ticket).exists()

    def test_non_critic_task_is_a_noop(self) -> None:
        ticket = _clean_delivered_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="x")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        assert record_returned_critic_verdict(task, {"critic_verdict": {"grader_identity": "critic-1"}}) == ""


class TestInstrumentationGap(TestCase):
    def test_a_raising_deterministic_predicate_is_recorded_as_instrumentation_gap(self) -> None:
        ticket = _clean_delivered_ticket()
        with patch("teatree.core.review.critic_rubric.spec_not_plan", side_effect=RuntimeError("boom")):
            _record_critic(ticket)
        finding = CriticFinding.objects.get(ticket=ticket, rubric_item="spec_not_plan")
        assert finding.status == CriticFinding.Status.INSTRUMENTATION_GAP


class TestStaleCleanup(TestCase):
    def test_a_now_clean_item_has_its_stale_finding_deleted(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        _record_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()
        _merge_audit(ticket)  # supply the merge evidence
        _record_critic(ticket)
        assert not CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()


class TestCriticFsmGate(TestCase):
    def test_advisory_delivery_records_a_finding_and_reaches_delivered(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        with _enforcement(live=False), self.captureOnCommitCallbacks(execute=False):
            ticket.mark_delivered()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_enforcing_delivery_is_refused_and_stays_retrospected(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        with _enforcement(live=True), pytest.raises(CriticGateError):
            ticket.mark_delivered()
        assert ticket.state == Ticket.State.RETROSPECTED

    def test_gate_is_load_bearing(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        neutralised = {**gate_registry._REGISTRY, ("gate", "critic"): lambda _ticket: None}
        with (
            _enforcement(live=True),
            patch.object(gate_registry, "_REGISTRY", neutralised),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.mark_delivered()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED


class TestEnforcingBlockFindingsSurviveRollback(TestCase):
    """The enforcing-mode rollback bug: findings must survive the delivery atomic the block rolls back.

    Drives the REAL ``execute_retrospect`` task (which wraps mark_delivered in
    ``transaction.atomic()`` exactly as production does). Under enforcement a flawed
    delivery blocks, the atomic rolls back — and the CriticFinding rows must still exist
    because ``_persist_critic_block`` re-records them on a fresh sibling transaction.
    """

    def test_findings_persist_after_the_enforcing_block(self) -> None:
        ticket = _clean_delivered_ticket()
        _strip_merge_evidence(ticket)
        with _enforcement(live=True), patch.object(core_tasks, "RetroExecutor") as retro:
            retro.return_value.run.return_value = RunnerResult(ok=True, detail="retro ok")
            result = core_tasks.execute_retrospect.func(ticket.pk)
        ticket.refresh_from_db()
        assert result["ok"] is False
        assert ticket.state == Ticket.State.RETROSPECTED  # the delivery was refused
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()  # survived rollback


class TestProductionRecordingPath(TestCase):
    """The LLM half must LAND through the REAL record_result_envelope, not only a direct call.

    The subtle production bug: a completed critic task flows through
    ``record_result_envelope``, which runs ``check_evidence`` BEFORE
    ``record_returned_critic_verdict``. Under the old ``phase="reviewing"`` wiring a
    critic result (only ``critic_verdict``) FAILED the reviewing evidence gate →
    ``_record_failure`` → the verdict was never recorded. The dedicated
    ``critic_reviewing`` phase (with its own ``critic_verdict`` evidence contract) closes it.
    """

    def test_verdict_and_finding_land_through_record_result_envelope(self) -> None:
        ticket = _clean_delivered_ticket()
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="c")
        assert dispatch is not None
        assert dispatch.task.phase == "critic_reviewing"
        with self.captureOnCommitCallbacks(execute=False):
            record_result_envelope(dispatch.task, _critic_envelope())
        assert CriticVerdict.objects.filter(ticket=ticket).exists()
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="coherence").exists()

    def test_red_before_the_reviewing_phase_wiring_records_nothing(self) -> None:
        # Prove the dedicated phase is load-bearing: on the OLD wiring (phase="reviewing")
        # the same critic result fails check_evidence("reviewing") -> _record_failure -> the
        # verdict is NEVER recorded. This is the exact production dead-path the fix closes.
        ticket = _clean_delivered_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="critic-dispatch")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="c",
        )
        CriticDispatch.objects.create(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, task=task)
        with self.captureOnCommitCallbacks(execute=False):
            record_result_envelope(task, _critic_envelope())
        assert not CriticVerdict.objects.filter(ticket=ticket).exists()
        assert not CriticFinding.objects.filter(ticket=ticket, rubric_item="coherence").exists()


class TestTransitionScoping(TestCase):
    """check_critic evaluates only the mark_delivered rubric subset (PR-1 transition seam).

    A deterministic item keyed to another transition, injected into the registry, must NOT
    be run by the mark_delivered gate — otherwise a plan/merge critic's items would fire at
    the wrong FSM point. Anti-vacuity: the same ticket makes the mark_delivered items fire,
    so the exclusion is a real filter, not an empty pass.
    """

    def test_run_critic_ignores_a_foreign_transition_item(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        foreign = CriticRubricItem(
            slug="plan_transition_probe",
            adversarial_question="a plan-transition item must not run at mark_delivered",
            kind=RubricKind.DETERMINISTIC,
            origin="north-star plan critic",
            predicate_path="teatree.core.review.critic_rubric.spec_not_plan",
            blocking=True,
            transition="plan",
        )
        with patch.object(critic_rubric, "CRITIC_RUBRIC", (*CRITIC_RUBRIC, foreign)):
            specs = run_critic(ticket)
        flagged = {spec.rubric_item for spec in specs}
        assert "plan_transition_probe" not in flagged
        assert "spec_not_plan" in flagged  # the mark_delivered items DID run — not a blanket-empty pass


class TestRegistration(TestCase):
    def test_critic_gate_is_registered(self) -> None:
        assert gate_registry.get_gate("critic") is check_critic
