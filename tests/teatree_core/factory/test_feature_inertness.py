"""The standing feature-inertness report (#4189).

The anchor is :class:`TestItRediscoversTheTwelve`: a report that cannot re-find the twelve
gates whose silent months motivated it is not working, whatever else it passes. Everything
else is the pair of directions that keep it from being a wall nobody reads — a deliberately
staged gate is a note, an undecided one is a fault, and evidence in the observable clears a
gate even while its flag is off.
"""

import datetime as dt
from dataclasses import replace
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.config.gate_evidence import GATE_EVIDENCE, ActivationIntent, GateEvidence, ObservableKind
from teatree.core.factory.feature_inertness import (
    FAULT_BANNER,
    KIND_NEVER_FIRED,
    KIND_UNOBSERVABLE,
    SATISFIER_MARKER,
    InertFeature,
    feature_inertness,
    render_inertness_report,
)
from teatree.core.models import ConfigSetting
from tests.factories import TicketFactory

# Fixed so the report's age arithmetic is pinned rather than drifting with the wall clock.
TODAY = dt.date(2026, 8, 9)

# #4189's list, verbatim. Not derived from the registry — a list derived from the thing under
# test would pass no matter what the registry drifted to.
THE_TWELVE = (
    "critic_gate_mode",
    "require_anti_vacuity_attestation",
    "require_debt_delta",
    "require_executed_repro",
    "require_integration_review",
    "require_merge_evidence",
    "require_merge_quality_verdict",
    "require_plan_adequacy",
    "require_review_context",
    "require_rubric_verification",
    "require_spec_coverage",
    "require_work_group_batch",
)


def _entry(
    setting: str,
    *,
    kind: ObservableKind = ObservableKind.TICKET_EXTRA,
    target: str = "review_context",
    shipped: dt.date = dt.date(2026, 1, 1),
    intent: ActivationIntent = ActivationIntent.UNDECIDED,
) -> GateEvidence:
    return GateEvidence(
        setting=setting,
        off_value=False,
        kind=kind,
        target=target,
        shipped=shipped,
        intent=intent,
        rationale="fixture — souliane/teatree#4189",
        satisfier="fixture satisfier",
    )


class TestItRediscoversTheTwelve(TestCase):
    """Criterion 3: the anti-vacuity anchor, against the live declaration."""

    def test_every_one_of_the_twelve_is_reported(self) -> None:
        reported = {finding.setting for finding in feature_inertness(now=TODAY)}
        assert set(THE_TWELVE) <= reported, f"lost from the report: {sorted(set(THE_TWELVE) - reported)}"

    def test_all_twelve_are_faults_not_notes(self) -> None:
        """Nobody ever decided to leave these off, so none may report as a deliberate stage."""
        notes = {f.setting for f in feature_inertness(now=TODAY) if not f.is_fault} & set(THE_TWELVE)
        assert notes == set()

    def test_each_detail_names_what_is_not_happening(self) -> None:
        """A finding whose detail only restates its kind gives the operator nothing to act on."""
        for finding in feature_inertness(now=TODAY):
            assert finding.kind in {KIND_NEVER_FIRED, KIND_UNOBSERVABLE}
            assert finding.detail != finding.kind
            assert "off for" in finding.detail

    def test_each_detail_names_the_next_action(self) -> None:
        """#4375: naming what never fired without naming what would MAKE it fire is unactionable."""
        for finding in feature_inertness(now=TODAY):
            head, _, satisfier = finding.detail.partition(SATISFIER_MARKER)
            assert satisfier.strip(), f"{finding.setting} names no way to satisfy it: {head}"


class TestTheNoteVersusFaultSplit(TestCase):
    """Criterion 2, both directions — a report that flags everything is as useless as one that flags nothing."""

    def test_an_undecided_gate_is_a_fault(self) -> None:
        registry = {"require_executed_repro": _entry("require_executed_repro")}
        (finding,) = feature_inertness(registry, now=TODAY)
        assert (finding.setting, finding.is_fault) == ("require_executed_repro", True)
        assert "nobody decided" in finding.detail

    def test_a_deliberately_staged_gate_is_a_note(self) -> None:
        registry = {"require_executed_repro": _entry("require_executed_repro", intent=ActivationIntent.STAGED)}
        (finding,) = feature_inertness(registry, now=TODAY)
        assert finding.is_fault is False
        assert "deliberately staged" in finding.detail


class TestEvidenceClearsAGateEvenWhileOff(TestCase):
    """Criterion 4: the evidence proves it ran, whatever the flag says."""

    def test_a_populated_ticket_extra_observable_is_not_reported(self) -> None:
        registry = {"require_review_context": _entry("require_review_context", target="review_context")}
        assert [f.setting for f in feature_inertness(registry, now=TODAY)] == ["require_review_context"]

        TicketFactory(extra={"review_context": {"work_item": "x"}})
        assert feature_inertness(registry, now=TODAY) == ()

    def test_a_populated_model_observable_is_not_reported(self) -> None:
        registry = {
            "require_plan_adequacy": _entry("require_plan_adequacy", kind=ObservableKind.MODEL, target="core.Ticket")
        }
        assert [f.setting for f in feature_inertness(registry, now=TODAY)] == ["require_plan_adequacy"]

        TicketFactory()
        assert feature_inertness(registry, now=TODAY) == ()

    def test_a_filter_keeps_a_shared_table_from_clearing_the_wrong_gate(self) -> None:
        """Two gates write ``CriticVerdict``; without the narrowing, either one clears both."""
        narrowed = GateEvidence(
            setting="require_plan_adequacy",
            off_value=False,
            kind=ObservableKind.MODEL,
            target="core.Ticket",
            shipped=dt.date(2026, 1, 1),
            intent=ActivationIntent.UNDECIDED,
            rationale="fixture — souliane/teatree#4189",
            satisfier="fixture satisfier",
            filters={"variant": "never-set"},
        )
        TicketFactory()
        assert [f.setting for f in feature_inertness({"require_plan_adequacy": narrowed}, now=TODAY)] == [
            "require_plan_adequacy"
        ]


class TestWhatTheReportDeliberatelyStaysQuietAbout(TestCase):
    def test_a_gate_younger_than_the_threshold_is_not_judged(self) -> None:
        registry = {"require_executed_repro": _entry("require_executed_repro", shipped=TODAY - dt.timedelta(days=6))}
        assert feature_inertness(registry, now=TODAY) == ()

    def test_a_gate_at_the_threshold_is_judged(self) -> None:
        registry = {"require_executed_repro": _entry("require_executed_repro", shipped=TODAY - dt.timedelta(days=7))}
        assert len(feature_inertness(registry, now=TODAY)) == 1

    def test_a_gate_enabled_globally_is_not_reported(self) -> None:
        ConfigSetting.objects.set_value("require_executed_repro", value=True)
        assert feature_inertness({"require_executed_repro": _entry("require_executed_repro")}, now=TODAY) == ()

    def test_a_gate_enabled_for_a_non_active_overlay_is_not_reported(self) -> None:
        """A gate on for ANY overlay is doing its job.

        The scope is deliberately not the active one: a global read resolves the active
        overlay by itself, so only a scope no ambient resolution reaches proves the
        per-scope union is load-bearing rather than decorative.
        """
        ConfigSetting.objects.set_value("require_executed_repro", value=True, scope="some-other-overlay")
        assert feature_inertness({"require_executed_repro": _entry("require_executed_repro")}, now=TODAY) == ()


class TestUnobservableGates(TestCase):
    def test_a_gate_with_no_observable_is_reported_as_unobservable(self) -> None:
        registry = {"require_debt_delta": _entry("require_debt_delta", kind=ObservableKind.NONE, target="")}
        (finding,) = feature_inertness(registry, now=TODAY)
        assert finding.kind == KIND_UNOBSERVABLE
        assert "nothing can ever prove it ran" in finding.detail

    def test_the_live_registry_marks_the_refusal_only_gates_unobservable(self) -> None:
        unobservable = {key for key, e in GATE_EVIDENCE.items() if e.kind is ObservableKind.NONE}
        assert unobservable == {
            "require_debt_delta",
            "require_merge_evidence",
            "require_reviewed_state_for_review_request",
            "require_work_group_batch",
        }


class TestTheReportNamesTheNextAction(TestCase):
    """#4375: ten gates sat undecided for up to 67 days against a report naming no next action.

    Both shapes carry it, and the never-fired one is the regression: its detail was assembled
    from ``_intent_clause`` alone, so the producer the declaration already recorded in prose was
    dropped on exactly the ten loudest lines.
    """

    def test_a_never_fired_line_carries_the_satisfier(self) -> None:
        entry = replace(_entry("require_executed_repro"), satisfier="`t3 <overlay> repro waive`")
        (finding,) = feature_inertness({entry.setting: entry}, now=TODAY)
        assert finding.kind == KIND_NEVER_FIRED
        assert finding.detail.endswith(f"{SATISFIER_MARKER}`t3 <overlay> repro waive`")

    def test_an_unobservable_line_carries_it_too(self) -> None:
        base = _entry("require_debt_delta", kind=ObservableKind.NONE, target="")
        entry = replace(base, satisfier="a diff with no new suppression")
        (finding,) = feature_inertness({entry.setting: entry}, now=TODAY)
        assert finding.kind == KIND_UNOBSERVABLE
        assert finding.detail.endswith(f"{SATISFIER_MARKER}a diff with no new suppression")

    def test_a_blank_satisfier_leaves_no_dangling_clause(self) -> None:
        entry = replace(_entry("require_executed_repro"), satisfier="   ")
        (finding,) = feature_inertness({entry.setting: entry}, now=TODAY)
        assert SATISFIER_MARKER not in finding.detail


class TestTheRenderedReport(TestCase):
    """The operator-facing half — proven from a fixture, both severities present."""

    def test_a_fault_is_loud_and_a_note_is_not(self) -> None:
        findings = (
            InertFeature("staged_gate", KIND_NEVER_FIRED, "off for 30d …", is_fault=False),
            InertFeature("undecided_gate", KIND_NEVER_FIRED, "off for 30d …", is_fault=True),
        )
        rendered = render_inertness_report(findings)
        assert FAULT_BANNER in rendered.splitlines()[0]
        assert "undecided_gate" in rendered.splitlines()[0]
        assert FAULT_BANNER not in rendered.splitlines()[1]

    def test_an_empty_report_says_so_rather_than_rendering_nothing(self) -> None:
        assert render_inertness_report(()) == "  (no gated feature is inert)"

    def test_the_cli_renders_the_live_report(self) -> None:
        out = StringIO()
        call_command("config_setting", "inert", stdout=out)
        assert out.getvalue().strip()
