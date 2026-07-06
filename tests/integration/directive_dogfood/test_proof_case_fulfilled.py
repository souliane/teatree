"""Test A — the capstone journey: CAPTURED → FULFILLED on real components (PR-8).

ONE FSM journey, an assertion per stage, driven end to end with the REAL settings
resolution, recorder gate, ratify question, baseline snapshot, ``ConfigSetting``
activation + resolver read-back, ``PullRequest`` probe, ``no_collateral_regression``
fold, and ONE real ``run_acceptance_tests`` subprocess. This is the merge-blocking
proof that the natural-language-directive → clean self-modification capability works.
Kept ONE test so the nested-pytest cost is paid once per suite run.
"""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.gates.pr_budget_gate import PrBudgetExceededError, check_pr_budget, resolve_pr_budget
from teatree.core.models import DeferredQuestion, DirectiveDispatch, FactoryScoreSnapshot, PullRequest, Ticket
from teatree.core.models.directive import Directive, DirectiveError
from teatree.loops.directive_loop.interpret import build_interpreter_contract
from tests.integration.directive_dogfood.exemplar import (
    EXEMPLAR_ENVELOPE,
    PROOF_CASE_TEXT,
    SCOPE,
    SETTING_KEY,
    enable_directive_loop_in_test_db,
    seed_critic_liveness,
    tick,
)


class TestProofCaseFulfilled(TestCase):
    def setUp(self) -> None:
        enable_directive_loop_in_test_db()
        seed_critic_liveness()

    def test_captured_to_fulfilled_end_to_end(self) -> None:
        # Stage 1 — capture via the real CLI surface, verbatim.
        call_command("directive", "capture", PROOF_CASE_TEXT, scope=SCOPE)
        directive = Directive.objects.get()
        assert directive.state == Directive.State.CAPTURED
        assert directive.raw_text == PROOF_CASE_TEXT

        # Stage 2 — the interpret dispatch is a real task on its own phase carrying the LIVE contract.
        assert tick().action == "interpret_dispatched"
        task = DirectiveDispatch.objects.get(directive=directive).task
        assert task is not None
        assert task.phase == "directive_interpreting"
        directive.refresh_from_db()
        assert task.execution_reason == build_interpreter_contract(directive)

        # Stage 3 — record the exemplar through the REAL recorder gate.
        assert record_returned_directive_interpretation(task, EXEMPLAR_ENVELOPE) == ""
        directive.refresh_from_db()
        assert directive.state == Directive.State.INTERPRETED
        assert directive.sketch is not None
        assert directive.sketch.kind == "activation_only"

        # Stage 4 — the human ratifies the full design; the gate has teeth in the integrated path.
        assert tick().action == "ratify_asked"
        directive.refresh_from_db()
        question = directive.ratify_question
        assert question is not None
        for shown in (SETTING_KEY, "pr_budget_gate", f"{SCOPE}=1", "N=2"):
            assert shown in question.question
        with pytest.raises(DirectiveError):
            directive.admit()  # an unconsumed ratify question cannot admit
        DeferredQuestion.consume(question.pk, answer="approve")
        assert tick().action == "admitted"

        # Stage 5 — activation_only skips IMPLEMENTING; the admission baseline is snapshotted for real.
        assert tick().action == "configuring"
        directive.refresh_from_db()
        baseline = directive.baseline_snapshot
        assert baseline is not None
        assert baseline.overlay == SCOPE
        assert baseline.recipe_sha  # provenance stamped
        assert FactoryScoreSnapshot.objects.filter(overlay=SCOPE).exists()

        # Stage 6 — byte-conformance writes the ConfigSetting row; it reads back through the resolver.
        assert tick().action == "verifying"
        assert resolve_pr_budget(SCOPE) == 1

        # Stage 7 — the user-visible outcome: the loop-applied activation arms the PR-2 tooth.
        pr_ticket = Ticket.objects.create(issue_url="https://github.com/acme/repo-a/issues/1", overlay=SCOPE)
        PullRequest.objects.create(
            ticket=pr_ticket,
            overlay=SCOPE,
            url="https://github.com/acme/repo-a/pull/1",
            repo="acme/repo-a",
            iid="1",
        )
        with pytest.raises(PrBudgetExceededError) as excinfo:
            check_pr_budget(pr_ticket, "acme/repo-a")
        assert "acme/repo-a/pull/1" in str(excinfo.value)
        assert "config_setting set max_open_prs_per_repo_per_ticket 0" in str(excinfo.value)
        check_pr_budget(pr_ticket, "acme/repo-b")  # a second repo is under budget — no raise

        # Stage 8 — past the horizon, all five evidence classes read by the REAL readers → FULFILLED.
        directive.refresh_from_db()
        assert directive.verify_started_at is not None
        past_horizon = directive.verify_started_at + timedelta(days=8)
        assert tick(now=past_horizon).action == "fulfilled"
        directive.refresh_from_db()
        assert directive.state == Directive.State.FULFILLED
        assert directive.decision_reason == "all five evidence classes green"
