"""Shared fixtures for the directive dogfood — the proof-case + real-component seams.

The proof-case is the north-star sentence "max 1 open MR per repo per ticket". Its
interpreter answer is the PR-2 ``pr_budget_gate`` mechanism, which already exists —
so the sketch is ``activation_only`` (configure, don't build). :data:`EXEMPLAR_ENVELOPE`
is that answer once, doing three jobs: the dogfood's recorded interpretation, the
metered eval's expected answer, and the exemplar the interpret doctrine already cites.
"""

import datetime as dt
from collections.abc import Callable

from django.core.management import call_command

from teatree.core.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.models import ConfigSetting, CriticVerdict, DeferredQuestion, DirectiveDispatch, Ticket
from teatree.core.models.directive import Directive
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.directive_loop.tick import DirectiveTickResult, TickSeams, run_tick
from teatree.loops.directive_loop.verify import VerifySeams
from teatree.loops.outer_loop.guards import MIN_CRITIC_SAMPLE, GuardSeams

#: The canonical north-star sentence. The customer overlay it would ship for is
#: carried by :data:`SCOPE`, never baked into the public repo as a brand string.
PROOF_CASE_TEXT = "Max 1 open MR per repo per ticket."

#: The always-registered dogfood overlay — the only one guaranteed present in CI, so
#: the recorder's ``validate_activation_scope`` resolves it.
SCOPE = "t3-teatree"

#: The PR-2 setting the proof-case constraint reduces to.
SETTING_KEY = "max_open_prs_per_repo_per_ticket"

#: A DB-free, pure-logic node the sketch's acceptance re-run exercises for real via
#: ONE ``run_acceptance_tests`` subprocess (evidence class 2). Chosen DB-free so the
#: nested pytest never provisions a database or touches any real store.
ACCEPTANCE_NODE_ID = (
    "tests/teatree_loops/directive_loop/test_verify.py::TestHorizonElapsed::test_elapsed_after_the_horizon"
)

#: The checked-in expected interpretation for the proof-case — one artifact, three
#: jobs (dogfood input, metered-eval expected answer, cited few-shot exemplar).
#: ``kind="activation_only"`` because duplication-first found PR-2's mechanism already
#: expresses the constraint; the sketch names the real setting, the real core seam,
#: and records the N=2-litmus rejected alternative.
EXEMPLAR_ENVELOPE: dict = {
    "directive_interpretation": {
        "interpreter_identity": "dogfood-fixture",
        "constraint_statement": "At most 1 open PR per (ticket, repo).",
        "sketch": {
            "kind": "activation_only",
            "setting_key": SETTING_KEY,
            "setting_type": "int",
            "neutral_default": 0,
            "policy_chokepoint": "src/teatree/core/gates/pr_budget_gate.py::check_pr_budget",
            "activation_scope": SCOPE,
            "activation_value": 1,
            "rejected_alternatives": [
                "overlay-local ship hook — fails N=2: a second overlay wanting max 2 would need code",
            ],
            "acceptance_tests": [ACCEPTANCE_NODE_ID],
            "refactors": [],
            "behavior_probe": "pr_budget_violations",
            "probe_none_reason": "",
        },
    },
}


def enable_directive_loop_in_test_db() -> None:
    """Turn the loop's two flag guards ON — as ``ConfigSetting`` rows in the TEST DB only.

    Global-scope rows, so ``get_effective_settings(SCOPE)`` (the loop's named-overlay
    resolution, which skips the env tier) reads them. Destroyed with the test DB;
    the production store is never touched.
    """
    ConfigSetting.objects.set_value("directive_loop_enabled", value=True)
    ConfigSetting.objects.set_value("factory_score_enabled", value=True)


def seed_critic_liveness() -> None:
    """Write ``MIN_CRITIC_SAMPLE`` real ``CriticVerdict`` rows so G2 sees a live critic.

    The real ``probe_critic_liveness`` counts every ``CriticVerdict`` row, so the
    dogfood exercises G2 for real (no ``critic_probe`` seam) rather than faking it.
    """
    # A non-forge scheme so `repo_namespaced_key` is blank — the seed ticket never
    # collides with the directive umbrella's synthetic interpret/impl tickets.
    ticket = Ticket.objects.create(issue_url="dogfood-smoke://critic-liveness-seed")
    for index in range(MIN_CRITIC_SAMPLE):
        CriticVerdict.objects.create(
            ticket=ticket, transition="plan", head_sha=f"{index:040d}", grader_identity="critic-seed", items=[]
        )


def _healthy_report() -> FactorySignalsReport:
    """A trusted signals report (no instrumentation gap) so G3 passes for real inputs."""
    row = SignalRow(
        provider_id="review_catch",
        kind="quant",
        reading=SignalReading(value=0.9, sample_size=50, window_days=28, status=SignalStatus.OK),
        direction=Direction.HIGHER_IS_BETTER,
        red_when=None,
        baseline_value=0.9,
        delta=0.0,
        tripped=False,
        verdict=SignalVerdict.OK,
    )
    return FactorySignalsReport(
        window_days=28, generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC), signals=[row], verdict=SignalVerdict.OK
    )


def dogfood_guard_seams() -> GuardSeams:
    """The two justified guard seams: G3 (healthy signals) + G4 (budget allow).

    G1/G1b resolve from the real ``ConfigSetting`` rows and G2 from the seeded real
    ``CriticVerdict`` rows; only the host-wide G3/G4 stay injected.
    """
    return GuardSeams(signal_report=_healthy_report(), budget=BudgetVerdict.allow())


def tick(
    *,
    now: dt.datetime | None = None,
    verify_seams: VerifySeams | None = None,
    merged_probe: Callable[[Directive], bool] | None = None,
) -> DirectiveTickResult:
    """Advance the oldest active directive ONE step through the real pipeline.

    ``settings=None`` so the guard chain resolves ``directive_loop_enabled`` /
    ``factory_score_enabled`` for real from the test-DB ``ConfigSetting`` rows.
    """
    seams = TickSeams(guards=dogfood_guard_seams(), verify_seams=verify_seams, merged_probe=merged_probe)
    return run_tick(overlay=SCOPE, now=now, settings=None, seams=seams)


def drive_activation_only_to_verifying() -> Directive:
    """Drive the proof-case ``activation_only`` directive CAPTURED → VERIFYING for real.

    The terse path Tests B/C share for the pre-violation setup: real CLI capture, real
    interpret dispatch, real recorder, real ratify consume, real activation write. The
    per-stage assertions are Test A's job — this only reaches the horizon.
    """
    call_command("directive", "capture", PROOF_CASE_TEXT, scope=SCOPE)
    directive = Directive.objects.get()
    tick()  # CAPTURED → interpret_dispatched
    task = DirectiveDispatch.objects.get(directive=directive).task
    record_returned_directive_interpretation(task, EXEMPLAR_ENVELOPE)  # → INTERPRETED
    tick()  # → ratify_asked
    directive.refresh_from_db()
    DeferredQuestion.consume(directive.ratify_question.pk, answer="approve")
    tick()  # RATIFY_PENDING → admitted
    tick()  # ADMITTED → configuring
    tick()  # CONFIGURING → verifying
    directive.refresh_from_db()
    return directive
