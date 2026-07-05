"""critic_gate (SELFCATCH-5): the autonomous user-proxy critic on mark_delivered.

Proves the whole self-catching contract. run_critic RECORDS a CriticFinding per
failing rubric item and DELETES a now-clean item's stale finding (latest verdict,
not an append-only log). Each of the 8 seeded classes, when EXHIBITED on an
otherwise-clean delivery, is caught by the critic (a finding recorded); the clean
twin records none. ADVISORY (flag off, the default) records findings while
mark_delivered still reaches DELIVERED — the critic ships dark and never wedges a
ticket. ENFORCING (flag on) raises CriticGateError so the ticket stays
RETROSPECTED. Anti-vacuity: with the critic gate NEUTRALISED the same flawed
delivery reaches DELIVERED even under enforcement — proving the critic is what
blocks. A predicate that RAISES is recorded as an instrumentation_gap, never a
silent pass.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates import critic_gate
from teatree.core.gates.critic_gate import check_critic, critic_enforcement_live, run_critic
from teatree.core.modelkit import gate_registry
from teatree.core.models import CriticFinding, CriticGateError, MergeAudit, MergeClear, PlanArtifact, Ticket
from teatree.core.models.plan_adequacy import all_negated_adequacy

_FORTY_HEX = "a" * 40
_RUBRIC_SLUGS = (
    "spec_not_plan",
    "done_not_done",
    "completeness",
    "coherence",
    "duplication",
    "deferred",
    "ignored_input",
    "unenforced_guarantee",
)


@contextlib.contextmanager
def _enforcement(*, live: bool) -> Iterator[None]:
    """Pin ``critic_gate_live`` (mirrors the merge_evidence_gate test's flag pin)."""
    with patch.object(critic_gate, "get_effective_settings", return_value=UserSettings(critic_gate_live=live)):
        yield


def _plan(ticket: Ticket) -> None:
    PlanArtifact.objects.create(
        ticket=ticket,
        plan_text="plan body",
        recorded_by="planner",
        base_sha=_FORTY_HEX,
        adequacy=dict(all_negated_adequacy("clean delivery")),
    )


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
    """A RETROSPECTED ticket clean on all 8 rubric items: adequate plan + merge audit + no claims."""
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
    _plan(ticket)
    _merge_audit(ticket)
    return ticket


def _perturb(ticket: Ticket, slug: str) -> None:
    """Break EXACTLY one rubric item on an otherwise-clean delivery."""
    if slug == "spec_not_plan":
        PlanArtifact.objects.filter(ticket=ticket).delete()
    elif slug == "done_not_done":
        MergeAudit.objects.filter(clear__ticket=ticket).delete()
        MergeClear.objects.filter(ticket=ticket).delete()
    elif slug == "completeness":
        ticket.extra = {"spec_coverage": {"acceptance_criteria": [{"id": "AC-2", "tests": []}]}}
    elif slug == "coherence":
        ticket.extra = {"critic": {"concept_merges": [{"merged": "companions into requires", "rationale": ""}]}}
    elif slug == "duplication":
        ticket.extra = {"critic": {"new_implementations": [{"symbol": "render_ref", "existing_search": ""}]}}
    elif slug == "deferred":
        ticket.extra = {"critic": {"deferrals": [{"what": "seam-parity checker", "ticket": ""}]}}
    elif slug == "ignored_input":
        ticket.extra = {"provided_inputs": ["https://example.com/paste/abc"], "addressed_inputs": []}
    elif slug == "unenforced_guarantee":
        ticket.extra = {"critic": {"guarantees": [{"claim": "never blocks", "test": ""}]}}
    ticket.save(update_fields=["extra"])


class TestRunCriticPerRubricItem(TestCase):
    """Anti-vacuity (a): each of the 8 classes, exhibited alone, is CAUGHT; the twin is not."""

    def test_each_exhibited_class_records_its_finding_and_clean_twin_records_none(self) -> None:
        for slug in _RUBRIC_SLUGS:
            with self.subTest(slug=slug):
                caught = _clean_delivered_ticket()
                _perturb(caught, slug)
                run_critic(caught)
                assert CriticFinding.objects.filter(ticket=caught, rubric_item=slug).exists(), f"{slug} not caught"

                twin = _clean_delivered_ticket()
                run_critic(twin)
                assert not CriticFinding.objects.filter(ticket=twin, rubric_item=slug).exists(), (
                    f"{slug} false positive"
                )


class TestRunCritic(TestCase):
    def test_clean_delivery_records_no_findings(self) -> None:
        ticket = _clean_delivered_ticket()
        assert run_critic(ticket) == []
        assert CriticFinding.objects.filter(ticket=ticket).count() == 0

    def test_all_eight_classes_caught_at_once(self) -> None:
        ticket = _clean_delivered_ticket()
        PlanArtifact.objects.filter(ticket=ticket).delete()
        MergeAudit.objects.filter(clear__ticket=ticket).delete()
        MergeClear.objects.filter(ticket=ticket).delete()
        ticket.extra = {
            "spec_coverage": {"acceptance_criteria": [{"id": "AC-2", "tests": []}]},
            "provided_inputs": ["https://example.com/paste/abc"],
            "addressed_inputs": [],
            "critic": {
                "concept_merges": [{"merged": "a into b", "rationale": ""}],
                "new_implementations": [{"symbol": "x", "existing_search": ""}],
                "deferrals": [{"what": "y", "ticket": ""}],
                "guarantees": [{"claim": "never", "test": ""}],
            },
        }
        ticket.save(update_fields=["extra"])
        findings = run_critic(ticket)
        assert {f.rubric_item for f in findings} == set(_RUBRIC_SLUGS)

    def test_a_now_clean_item_has_its_stale_finding_deleted(self) -> None:
        ticket = _clean_delivered_ticket()
        MergeAudit.objects.filter(clear__ticket=ticket).delete()
        MergeClear.objects.filter(ticket=ticket).delete()
        run_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()
        # The delivery is fixed (merge evidence supplied); the critic re-runs.
        _merge_audit(ticket)
        run_critic(ticket)
        assert not CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_a_raising_predicate_is_recorded_as_instrumentation_gap(self) -> None:
        ticket = _clean_delivered_ticket()
        with patch("teatree.core.critic_rubric.coherence", side_effect=RuntimeError("probe blew up")):
            run_critic(ticket)
        finding = CriticFinding.objects.get(ticket=ticket, rubric_item="coherence")
        assert finding.status == CriticFinding.Status.INSTRUMENTATION_GAP


class TestEnforcementGating(TestCase):
    def test_advisory_records_findings_but_does_not_raise(self) -> None:
        ticket = _clean_delivered_ticket()
        _perturb(ticket, "done_not_done")
        with _enforcement(live=False):
            check_critic(ticket)  # no raise
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_enforcing_raises_on_a_finding(self) -> None:
        ticket = _clean_delivered_ticket()
        _perturb(ticket, "done_not_done")
        with _enforcement(live=True), pytest.raises(CriticGateError) as exc:
            check_critic(ticket)
        assert "done_not_done" in str(exc.value)
        assert "critic_gate_live false" in str(exc.value)  # names the kill-switch escape
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_enforcing_passes_a_clean_delivery(self) -> None:
        ticket = _clean_delivered_ticket()
        with _enforcement(live=True):
            check_critic(ticket)  # no raise — nothing to block

    def test_enforcement_resolver_reads_the_flag(self) -> None:
        with patch.object(critic_gate, "get_effective_settings", return_value=UserSettings(critic_gate_live=True)):
            assert critic_enforcement_live("t3-teatree") is True
        with patch.object(critic_gate, "get_effective_settings", return_value=UserSettings(critic_gate_live=False)):
            assert critic_enforcement_live("t3-teatree") is False


class TestCriticFsmGate(TestCase):
    """The critic wired at the mark_delivered FSM chokepoint."""

    def test_advisory_delivery_records_a_finding_and_still_reaches_delivered(self) -> None:
        ticket = _clean_delivered_ticket()
        _perturb(ticket, "done_not_done")
        with _enforcement(live=False), self.captureOnCommitCallbacks(execute=False):
            ticket.mark_delivered()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_enforcing_delivery_is_refused_and_stays_retrospected(self) -> None:
        ticket = _clean_delivered_ticket()
        _perturb(ticket, "done_not_done")
        with _enforcement(live=True), pytest.raises(CriticGateError):
            ticket.mark_delivered()
        assert ticket.state == Ticket.State.RETROSPECTED  # the transition did NOT advance
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").exists()

    def test_clean_delivery_reaches_delivered_under_enforcement(self) -> None:
        ticket = _clean_delivered_ticket()
        with _enforcement(live=True), self.captureOnCommitCallbacks(execute=False):
            ticket.mark_delivered()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED

    def test_gate_is_load_bearing(self) -> None:
        """Anti-vacuity: with the critic gate neutralised, the same flawed delivery advances.

        If this passes while ``test_enforcing_delivery_is_refused_and_stays_retrospected``
        also passes, the critic is genuinely the thing blocking the flawed delivery.
        """
        ticket = _clean_delivered_ticket()
        _perturb(ticket, "done_not_done")
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


class TestRegistration(TestCase):
    def test_critic_gate_is_registered(self) -> None:
        assert gate_registry.get_gate("critic") is check_critic
