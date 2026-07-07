"""The design critic (north-star PR-5): the generic-vs-hack judgment at PLAN time.

The four ``transition="plan"`` LLM items (``generality`` / ``sketch_conformance`` /
``convention_fit`` / ``refactor_honesty``) are judged by the async critic (mocked here
as a recorded ``CriticVerdict``, exactly like the merge-quality tests) and this ADVISORY
gate mirrors the FAIL items into ``CriticFinding(transition="plan")`` — it never blocks
(the deterministic ``mechanism_conforms`` section is the teeth). Anti-vacuity (b):

- a one-off/special-case sketch → ``generality`` FAIL verdict → finding recorded.
- a clean generic core mechanism → all pass → no finding.

Transition scoping, dark-inert cost-parity, and the ``Ticket.plan()`` wiring are pinned
alongside — the design critic never fires at mark_delivered/merge, and is a strict no-op
(no dispatch) while its arming flag ``directive_loop_enabled`` is dark (#104: the
advisory-only design critic folds into the directive-loop flag).
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates import design_critic_gate
from teatree.core.gates.design_critic_gate import (
    build_design_contract,
    check_design_critic,
    covering_verdict,
    design_critic_armed,
    plan_head_sha,
    record_design_findings,
)
from teatree.core.models import CriticDispatch, CriticFinding, CriticVerdict, Directive, PlanArtifact, Ticket
from teatree.core.models.mechanism_sketch import MechanismSketch

_FORTY_HEX = "a" * 40
_OTHER_HEX = "b" * 40
_PLAN_SLUGS = ("generality", "sketch_conformance", "convention_fit", "refactor_honesty")


def _sketch() -> MechanismSketch:
    return MechanismSketch(
        kind="setting_policy_gate",
        setting_key="max_open_prs_per_repo_per_ticket",
        setting_type="int",
        neutral_default=0,
        policy_chokepoint="src/teatree/core/gates/pr_budget_gate.py::check_pr_budget",
        activation_scope="example-overlay",
        activation_value=1,
        rejected_alternatives=("an overlay-local hook — fails the N=2 litmus",),
    )


def _conforming_manifest() -> dict:
    """A plan adequacy carrying a mechanism_placement that conforms to :func:`_sketch`."""
    sketch = _sketch()
    return {
        "mechanism_placement": {
            "setting_key": sketch.setting_key,
            "neutral_default": sketch.neutral_default,
            "policy_chokepoint": sketch.policy_chokepoint,
            "activation_scope": sketch.activation_scope,
            "activation_value": sketch.activation_value,
            "rejected_alternatives": list(sketch.rejected_alternatives),
        }
    }


def _directive_ticket(*, with_sketch: bool = True, with_plan: bool = True) -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLANNED)
    directive = Directive.objects.capture("max 1 open PR per repo per ticket", source=Directive.Source.CLI)
    if with_sketch:
        directive.mechanism_sketch = _sketch().to_dict()
        directive.save(update_fields=["mechanism_sketch"])
    directive.ticket = ticket
    directive.save(update_fields=["ticket"])
    if with_plan:
        PlanArtifact.objects.create(
            ticket=ticket, plan_text="plan body", recorded_by="planner", base_sha=_FORTY_HEX, adequacy={}
        )
    return ticket


def _record_plan_verdict(ticket: Ticket, *, items: list[dict], head_sha: str = _FORTY_HEX) -> CriticVerdict:
    return CriticVerdict.record_from_envelope(
        ticket=ticket,
        transition="plan",
        head_sha=head_sha,
        envelope={"grader_identity": "design-critic-7", "items": items},
    )


def _pass(slug: str) -> dict:
    return {"slug": slug, "status": "pass", "citation": f"inspected {slug} at src/x.py:1, clean generic mechanism"}


def _fail(slug: str, citation: str) -> dict:
    return {"slug": slug, "status": "fail", "citation": citation}


def _clean_items() -> list[dict]:
    return [_pass(slug) for slug in _PLAN_SLUGS]


def _one_fail(slug: str, citation: str) -> list[dict]:
    """A verdict where *slug* FAILs and every other plan item passes."""
    return [_fail(item, citation) if item == slug else _pass(item) for item in _PLAN_SLUGS]


@contextlib.contextmanager
def _live() -> Iterator[None]:
    # #104: the design critic is armed BY the directive-loop flag — it is advisory-only
    # and fires only for directive-linked tickets, so it carries no independent switch.
    with patch.object(
        design_critic_gate, "get_effective_settings", return_value=UserSettings(directive_loop_enabled=True)
    ):
        yield


class TestArmedResolution(TestCase):
    def test_dark_by_default(self) -> None:
        assert design_critic_armed("t3-teatree") is False

    def test_armed_when_directive_loop_enabled(self) -> None:
        with _live():
            assert design_critic_armed("t3-teatree") is True


class TestPlanHeadSha(TestCase):
    def test_reads_the_plan_base_sha(self) -> None:
        assert plan_head_sha(_directive_ticket()) == _FORTY_HEX

    def test_no_plan_is_empty(self) -> None:
        assert plan_head_sha(_directive_ticket(with_plan=False)) == ""


class TestAdvisoryFindings(TestCase):
    """Anti-vacuity (b): a one-off/hack verdict → finding; a clean generic mechanism → none."""

    def test_a_one_off_verdict_records_a_generality_finding(self) -> None:
        ticket = _directive_ticket()
        _record_plan_verdict(ticket, items=_one_fail("generality", "overlay-local hook — a 2nd overlay needs code"))
        with _live():
            check_design_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, transition="plan", rubric_item="generality").exists()

    def test_a_clean_generic_verdict_records_no_finding(self) -> None:
        ticket = _directive_ticket()
        _record_plan_verdict(ticket, items=_clean_items())
        with _live():
            check_design_critic(ticket)
        assert not CriticFinding.objects.filter(ticket=ticket, transition="plan").exists()

    def test_an_uncited_pass_is_downgraded_and_recorded(self) -> None:
        ticket = _directive_ticket()
        items = [{"slug": "generality", "status": "pass", "citation": ""}, *[_pass(s) for s in _PLAN_SLUGS[1:]]]
        _record_plan_verdict(ticket, items=items)
        with _live():
            check_design_critic(ticket)
        finding = CriticFinding.objects.get(ticket=ticket, transition="plan", rubric_item="generality")
        assert finding.status == CriticFinding.Status.INSTRUMENTATION_GAP

    def test_a_re_judged_clean_head_clears_the_stale_finding(self) -> None:
        ticket = _directive_ticket()
        _record_plan_verdict(ticket, items=_one_fail("generality", "hack"))
        with _live():
            check_design_critic(ticket)
        assert CriticFinding.objects.filter(ticket=ticket, transition="plan").exists()

        _record_plan_verdict(ticket, items=_clean_items())  # re-judged clean at the same head
        with _live():
            check_design_critic(ticket)
        assert not CriticFinding.objects.filter(ticket=ticket, transition="plan").exists()

    def test_record_design_findings_is_the_direct_writer(self) -> None:
        ticket = _directive_ticket()
        verdict = _record_plan_verdict(ticket, items=_one_fail("convention_fit", "parallel mechanism"))
        record_design_findings(verdict, ticket=ticket, head_sha=_FORTY_HEX)
        assert CriticFinding.objects.filter(ticket=ticket, transition="plan", rubric_item="convention_fit").exists()


class TestAdvisoryNeverBlocks(TestCase):
    def test_check_design_critic_returns_none_even_on_a_fail_verdict(self) -> None:
        ticket = _directive_ticket()
        _record_plan_verdict(ticket, items=_one_fail("generality", "hack"))
        with _live():
            assert check_design_critic(ticket) is None  # advisory — records a finding, never raises/blocks


class TestArming(TestCase):
    def test_no_verdict_arms_the_async_critic_at_the_plan_head(self) -> None:
        ticket = _directive_ticket()
        with _live():
            check_design_critic(ticket)
        dispatch = CriticDispatch.objects.filter(ticket=ticket, transition="plan", head_sha=_FORTY_HEX).first()
        assert dispatch is not None
        assert dispatch.task is not None
        assert dispatch.task.phase == "critic_reviewing"

    def test_a_covering_verdict_arms_nothing(self) -> None:
        ticket = _directive_ticket()
        _record_plan_verdict(ticket, items=_clean_items())
        with _live():
            check_design_critic(ticket)
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="plan").exists()


class TestNoOpConditions(TestCase):
    def test_dark_flag_is_a_strict_noop_no_dispatch(self) -> None:
        # Cost parity while dark: no CriticDispatch/Task created at all.
        ticket = _directive_ticket()
        check_design_critic(ticket)  # flag off (default)
        assert not CriticDispatch.objects.filter(ticket=ticket).exists()

    def test_ordinary_ticket_is_a_noop(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLANNED)
        with _live():
            check_design_critic(ticket)
        assert not CriticDispatch.objects.filter(ticket=ticket).exists()

    def test_directive_without_a_sketch_is_a_noop(self) -> None:
        ticket = _directive_ticket(with_sketch=False)
        with _live():
            check_design_critic(ticket)
        assert not CriticDispatch.objects.filter(ticket=ticket).exists()

    def test_directive_without_a_bound_plan_is_a_noop(self) -> None:
        ticket = _directive_ticket(with_plan=False)
        with _live():
            check_design_critic(ticket)
        assert not CriticDispatch.objects.filter(ticket=ticket).exists()


class TestTransitionScoping(TestCase):
    """The plan items are keyed to ``plan`` — a mark_delivered/merge verdict never covers the design gate."""

    def test_a_merge_verdict_does_not_cover_the_plan_gate(self) -> None:
        ticket = _directive_ticket()
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha=_FORTY_HEX,
            envelope={"grader_identity": "critic-7", "items": _clean_items()},
        )
        assert covering_verdict(ticket, _FORTY_HEX) is None  # merge verdict is not a plan verdict

    def test_a_verdict_at_a_different_head_does_not_cover(self) -> None:
        ticket = _directive_ticket()
        _record_plan_verdict(ticket, items=_clean_items(), head_sha=_OTHER_HEX)
        assert covering_verdict(ticket, _FORTY_HEX) is None


class TestDispatchContract(TestCase):
    def test_the_contract_names_every_plan_slug_and_the_ratified_sketch(self) -> None:
        ticket = _directive_ticket()
        contract = build_design_contract(ticket, _FORTY_HEX)
        for slug in _PLAN_SLUGS:
            assert slug in contract, slug
        assert "max_open_prs_per_repo_per_ticket" in contract  # the ratified sketch is in front of the critic
        assert "N=2 litmus" in contract


class TestPlanTransitionWiring(TestCase):
    """``Ticket.plan()`` runs the advisory design critic for a directive ticket when live."""

    def _planned_directive_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        directive = Directive.objects.capture("cap the PRs", source=Directive.Source.CLI)
        directive.mechanism_sketch = _sketch().to_dict()
        directive.ticket = ticket
        directive.save(update_fields=["mechanism_sketch", "ticket"])
        # plan() → schedule_coding() → plan_currency_gate, whose directive mechanism teeth
        # now run UNCONDITIONALLY (H3) — so a directive plan must CONFORM to its sketch to
        # reach coder dispatch, regardless of require_plan_adequacy. A conforming plan keeps
        # these tests focused on the design-critic arming they exercise.
        PlanArtifact.objects.create(
            ticket=ticket,
            plan_text="plan",
            recorded_by="t3:planner",
            base_sha=_FORTY_HEX,
            adequacy=_conforming_manifest(),
        )
        return ticket

    def test_plan_arms_the_design_critic_when_live(self) -> None:
        ticket = self._planned_directive_ticket()
        with _live(), self.captureOnCommitCallbacks(execute=True):
            ticket.plan()
        assert CriticDispatch.objects.filter(ticket=ticket, transition="plan", head_sha=_FORTY_HEX).exists()

    def test_plan_is_inert_when_dark(self) -> None:
        ticket = self._planned_directive_ticket()
        with self.captureOnCommitCallbacks(execute=True):
            ticket.plan()  # flag off (default)
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="plan").exists()
