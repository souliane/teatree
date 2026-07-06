"""The VERIFY phase — gather evidence, decide, keep-only-if-verified (north-star PR-7).

Anti-vacuity: the all-green path FULFILS; a verify FAIL (any evidence class) parks in
REVERT_PENDING AND rolls the config back instantly (RED-before). The real readers are
exercised against durable state; the decision path uses injectable seams.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import (
    ConfigSetting,
    CriticFinding,
    DeferredQuestion,
    Directive,
    FactoryScoreSnapshot,
    PullRequest,
    Ticket,
)
from teatree.core.models.critic_finding import CriticFindingSpec
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop import verify
from teatree.loops.directive_loop.verify import VerifySeams, gather_evidence, horizon_elapsed, verify_and_decide
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope

_SCOPE = "t3-teatree"
_KEY = "max_open_prs_per_repo_per_ticket"


def _snapshot() -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.create(
        overlay="", window_days=7, recipe_sha="s", aggregate=0.7, verdict="ok", coverage=1.0, coverage_floor=0.6
    )


def _verifying(**sketch_over: object) -> Directive:
    directive = Directive.objects.capture("max 1 MR", source=Directive.Source.CLI, scope_overlay=_SCOPE)
    directive.record_interpretation(sketch_from_envelope(valid_envelope(**sketch_over)), constraint_statement="c")
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    directive.skip_to_configuring(baseline_snapshot=_snapshot())
    directive.begin_verifying()
    return directive


def _all_green_seams() -> VerifySeams:
    return VerifySeams(
        activation_reader=lambda _d: True,
        acceptance_reader=lambda _d: True,
        probe_reader=lambda _d, _n: "",
        regression_reader=lambda _d: "",
        critic_findings_reader=lambda _d: 0,
    )


class TestHorizonElapsed:
    def test_not_elapsed_before_the_horizon(self) -> None:
        directive = Directive(verify_started_at=timezone.now())
        assert horizon_elapsed(directive, verify_days=7, now=timezone.now()) is False

    def test_elapsed_after_the_horizon(self) -> None:
        directive = Directive(verify_started_at=timezone.now() - timedelta(days=8))
        assert horizon_elapsed(directive, verify_days=7, now=timezone.now()) is True

    def test_unarmed_clock_is_never_elapsed(self) -> None:
        assert horizon_elapsed(Directive(), verify_days=7, now=timezone.now()) is False


class TestVerifyAndDecide(TestCase):
    def test_all_five_green_fulfils(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        decision = verify_and_decide(directive, seams=_all_green_seams())
        assert decision.fulfilled is True
        assert directive.state == Directive.State.FULFILLED

    def test_verify_fail_parks_in_revert_pending_and_rolls_config_back(self) -> None:
        # RED-before instant rollback: a collateral regression → REVERT_PENDING AND the
        # overlay ConfigSetting is cleared the moment the directive is flagged for revert.
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        seams = VerifySeams(
            activation_reader=lambda _d: True,
            acceptance_reader=lambda _d: True,
            probe_reader=lambda _d, _n: "",
            regression_reader=lambda _d: "review_catch regressed",
            critic_findings_reader=lambda _d: 0,
        )
        decision = verify_and_decide(directive, seams=seams)
        assert decision.fulfilled is False
        assert directive.state == Directive.State.REVERT_PENDING
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) is None

    def test_an_open_critic_finding_blocks_fulfilment(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        seams = VerifySeams(
            activation_reader=lambda _d: True,
            acceptance_reader=lambda _d: True,
            probe_reader=lambda _d, _n: "",
            regression_reader=lambda _d: "",
            critic_findings_reader=lambda _d: 2,
        )
        assert verify_and_decide(directive, seams=seams).fulfilled is False
        assert directive.state == Directive.State.REVERT_PENDING


class TestRealReaders(TestCase):
    def test_activation_reader_reads_back_through_the_resolver(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        assert verify._activation_live(directive) is False  # not yet written
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        assert verify._activation_live(directive) is True

    def test_acceptance_reader_empty_nodes_is_green_without_running(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        assert verify._acceptance_green(directive) is True

    def test_acceptance_reader_runs_named_nodes(self) -> None:
        directive = _verifying()  # setting_policy_gate names acceptance tests
        with patch("teatree.loops.directive_loop.verify.run_acceptance_tests", return_value=True) as run:
            assert verify._acceptance_green(directive) is True
        run.assert_called_once()

    def test_critic_findings_reader_counts_the_ticket_findings(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        ticket = Ticket.objects.create(issue_url="https://e.com/mech", role=Ticket.Role.AUTHOR)
        directive.ticket = ticket
        directive.save(update_fields=["ticket"])
        assert verify._open_critic_findings(directive) == 0
        CriticFinding.record(
            ticket=ticket, transition="plan", spec=CriticFindingSpec(rubric_item="generality", detail="d")
        )
        assert verify._open_critic_findings(directive) == 1

    def test_readers_return_false_with_no_sketch(self) -> None:
        bare = Directive.objects.capture("x", source=Directive.Source.CLI)
        assert verify._activation_live(bare) is False
        assert verify._acceptance_green(bare) is False

    def test_probe_reader_empty_when_the_sketch_names_no_probe(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        assert verify._probe_finding(directive, timezone.now()) == ""

    def test_probe_reader_flags_an_unknown_probe(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[], behavior_probe="nonexistent_probe")
        assert "not in the probe catalog" in verify._probe_finding(directive, timezone.now())

    def test_probe_reader_returns_a_catalog_finding(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[], behavior_probe="pr_budget_violations")
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        pr_ticket = Ticket.objects.create(issue_url="https://e.com/pt", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        for iid in ("1", "2"):
            PullRequest.objects.create(
                ticket=pr_ticket, overlay=_SCOPE, url=f"https://github.com/o/r/pull/{iid}", repo="o/r", iid=iid
            )
        assert "open PRs" in verify._probe_finding(directive, timezone.now())

    def test_collateral_regression_reader_needs_a_baseline(self) -> None:
        bare = Directive.objects.capture("x", source=Directive.Source.CLI, scope_overlay=_SCOPE)
        assert "no admission baseline" in verify._collateral_regression(bare)

    def test_gather_evidence_uses_the_real_readers_by_default(self) -> None:
        directive = _verifying(kind="activation_only", acceptance_tests=[])
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        evidence = gather_evidence(directive)
        assert evidence.activation_live is True
        assert evidence.acceptance_green is True
        assert evidence.open_critic_findings == 0
