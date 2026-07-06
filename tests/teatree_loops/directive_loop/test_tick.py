"""The tick FSM dispatch — every advance branch + the full happy path (north-star PR-7).

Drives the post-ADMITTED arc via injected seams (no live critic, real merge, real
pytest, or real clock): the full happy path ADMITTED → implement → configure → verify
→ FULFILLED, and keep-only-if-verified (a verify FAIL → REVERT_PENDING, not FULFILLED).
"""

import datetime as dt
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.core.models import ConfigSetting, DeferredQuestion, Directive, Ticket
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.directive_loop import guards
from teatree.loops.directive_loop.tick import TickSeams, run_tick
from teatree.loops.directive_loop.verify import VerifySeams
from teatree.loops.outer_loop.guards import CriticLiveness, GuardSeams
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope

_SCOPE = "t3-teatree"
_KEY = "max_open_prs_per_repo_per_ticket"


def _live_critic() -> CriticLiveness:
    return CriticLiveness(live=True, verdict_count=guards.__dict__.get("MIN_CRITIC_SAMPLE", 5) or 5)


def _healthy_report() -> FactorySignalsReport:
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


def _open_settings() -> SimpleNamespace:
    return SimpleNamespace(directive_loop_enabled=True, factory_score_enabled=True, directive_verify_days=7)


def _all_green_verify() -> VerifySeams:
    return VerifySeams(
        activation_reader=lambda _d: True,
        acceptance_reader=lambda _d: True,
        probe_reader=lambda _d, _n: "",
        regression_reader=lambda _d: "",
        critic_findings_reader=lambda _d: 0,
    )


def _seams(*, merged: bool | None = None, verify_seams: VerifySeams | None = None) -> TickSeams:
    return TickSeams(
        guards=GuardSeams(critic_probe=_live_critic, signal_report=_healthy_report(), budget=BudgetVerdict.allow()),
        merged_probe=(lambda _d: merged) if merged is not None else None,
        verify_seams=verify_seams,
    )


def _admitted(**sketch_over: object) -> Directive:
    directive = Directive.objects.capture("max 1 MR", source=Directive.Source.CLI, scope_overlay=_SCOPE)
    directive.record_interpretation(sketch_from_envelope(valid_envelope(**sketch_over)), constraint_statement="c")
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    return directive


def _tick(**kw: object) -> object:
    return run_tick(settings=_open_settings(), seams=_seams(**kw))


class TestIntakeBranches(TestCase):
    def test_captured_dispatches_the_interpreter(self) -> None:
        Directive.objects.capture("do X", source=Directive.Source.CLI)
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "interpret_dispatched"

    def test_interpreted_asks_ratification(self) -> None:
        directive = Directive.objects.capture("do X", source=Directive.Source.CLI)
        directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="c")
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "ratify_asked"

    def test_ratify_pending_admits_on_approval(self) -> None:
        directive = Directive.objects.capture("do X", source=Directive.Source.CLI)
        directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="c")
        question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
        directive.attach_ratification(question)
        DeferredQuestion.consume(question.pk, answer="approve")
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "admitted"

    def test_clarifying_waits_then_reinterprets_when_answered(self) -> None:
        directive = Directive.objects.capture("ambiguous", source=Directive.Source.CLI)
        directive.mark_clarifying()
        clarify = DeferredQuestion.record("which?", options_hash=f"directive_clarify:{directive.pk}:0:0")
        first = run_tick(settings=_open_settings(), seams=_seams())
        assert first.action == "waiting"
        assert first.reason == "awaiting_clarification"
        DeferredQuestion.consume(clarify.pk, answer="this one")
        second = run_tick(settings=_open_settings(), seams=_seams())
        assert second.action == "reinterpret_dispatched"


class TestExecutionBranches(TestCase):
    def test_admitted_setting_policy_gate_implements(self) -> None:
        _admitted()
        assert _tick().action == "implementing"

    def test_admitted_activation_only_skips_to_configuring(self) -> None:
        _admitted(kind="activation_only", acceptance_tests=[])
        assert _tick().action == "configuring"

    def test_implementing_waits_until_merged_then_configures(self) -> None:
        _admitted()
        run_tick(settings=_open_settings(), seams=_seams())  # → IMPLEMENTING
        waiting = run_tick(settings=_open_settings(), seams=_seams(merged=False))
        assert waiting.action == "waiting"
        assert waiting.reason == "implement_in_flight"
        configuring = run_tick(settings=_open_settings(), seams=_seams(merged=True))
        assert configuring.action == "configuring"

    def test_configuring_applies_activation_and_arms_verify(self) -> None:
        directive = _admitted(kind="activation_only", acceptance_tests=[])
        run_tick(settings=_open_settings(), seams=_seams())  # ADMITTED → CONFIGURING
        result = run_tick(settings=_open_settings(), seams=_seams())  # CONFIGURING → VERIFYING
        assert result.action == "verifying"
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) == 1
        directive.refresh_from_db()
        assert directive.verify_started_at is not None

    def test_empty_scope_configures_as_a_no_op_and_advances_to_verify(self) -> None:
        # A global (empty-scope) mechanism configures as a no-op success — the tick
        # advances to VERIFYING, never soft-locks.
        directive = _admitted(kind="activation_only", acceptance_tests=[], activation_scope="")
        run_tick(settings=_open_settings(), seams=_seams())  # ADMITTED → CONFIGURING
        result = run_tick(settings=_open_settings(), seams=_seams())  # CONFIGURING → VERIFYING
        assert result.action == "verifying"
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.VERIFYING

    def test_configure_refusal_escalates_to_revert_pending_not_soft_lock(self) -> None:
        # A persistent configure refusal (a setting_policy_gate whose implementation
        # never added the setting → read-back mismatch) escalates to a human-asked
        # revert, never an infinite waiting that holds the slot.
        directive = _admitted(setting_key="never_added_setting_xyz")  # setting_policy_gate default
        run_tick(settings=_open_settings(), seams=_seams())  # ADMITTED → IMPLEMENTING
        directive.refresh_from_db()
        Ticket.objects.filter(pk=directive.ticket_id).update(state=Ticket.State.MERGED)
        run_tick(settings=_open_settings(), seams=_seams())  # IMPLEMENTING → CONFIGURING
        result = run_tick(settings=_open_settings(), seams=_seams())  # CONFIGURING refuses → REVERT_PENDING
        assert result.action == "revert_pending"
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERT_PENDING
        assert ConfigSetting.objects.get_effective("never_added_setting_xyz", scope=_SCOPE) is None
        # The next tick visibly asks the human (no dead-end).
        ask = run_tick(settings=_open_settings(), seams=_seams())
        assert ask.action == "revert_asked"

    def test_implementing_real_merged_probe_reads_the_ticket_state(self) -> None:
        directive = _admitted()
        run_tick(settings=_open_settings(), seams=_seams())  # → IMPLEMENTING
        directive.refresh_from_db()
        Ticket.objects.filter(pk=directive.ticket_id).update(state=Ticket.State.MERGED)
        # No merged_probe injected → the real _ticket_merged reads the ticket state.
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "configuring"


class TestFullHappyPathAndKeepRule(TestCase):
    def _to_verifying(self, **over: object) -> Directive:
        directive = _admitted(**over)
        run_tick(settings=_open_settings(), seams=_seams())  # ADMITTED → CONFIGURING (activation_only)
        run_tick(settings=_open_settings(), seams=_seams())  # CONFIGURING → VERIFYING
        directive.refresh_from_db()
        return directive

    def test_verifying_waits_before_the_horizon(self) -> None:
        self._to_verifying(kind="activation_only", acceptance_tests=[])
        result = run_tick(settings=_open_settings(), seams=_seams(verify_seams=_all_green_verify()))
        assert result.action == "waiting"
        assert result.reason == "horizon_not_elapsed"

    def test_full_happy_path_reaches_fulfilled(self) -> None:
        directive = self._to_verifying(kind="activation_only", acceptance_tests=[])
        Directive.objects.filter(pk=directive.pk).update(verify_started_at=timezone.now() - dt.timedelta(days=30))
        result = run_tick(settings=_open_settings(), seams=_seams(verify_seams=_all_green_verify()))
        assert result.action == "fulfilled"
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.FULFILLED

    def test_verify_fail_reverts_not_fulfils(self) -> None:
        # keep-only-if-verified: a collateral regression → REVERT_PENDING, config rolled back.
        directive = self._to_verifying(kind="activation_only", acceptance_tests=[])
        Directive.objects.filter(pk=directive.pk).update(verify_started_at=timezone.now() - dt.timedelta(days=30))
        failing = VerifySeams(
            activation_reader=lambda _d: True,
            acceptance_reader=lambda _d: True,
            probe_reader=lambda _d, _n: "",
            regression_reader=lambda _d: "review_catch regressed",
            critic_findings_reader=lambda _d: 0,
        )
        result = run_tick(settings=_open_settings(), seams=_seams(verify_seams=failing))
        assert result.action == "revert_pending"
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.REVERT_PENDING
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) is None

    def test_revert_pending_asks_then_awaits_a_human(self) -> None:
        directive = self._to_verifying(kind="activation_only", acceptance_tests=[])
        Directive.objects.filter(pk=directive.pk).update(verify_started_at=timezone.now() - dt.timedelta(days=30))
        failing = VerifySeams(
            activation_reader=lambda _d: False,
            acceptance_reader=lambda _d: True,
            probe_reader=lambda _d, _n: "",
            regression_reader=lambda _d: "",
            critic_findings_reader=lambda _d: 0,
        )
        run_tick(settings=_open_settings(), seams=_seams(verify_seams=failing))  # → REVERT_PENDING
        first = run_tick(settings=_open_settings(), seams=_seams())
        assert first.action == "revert_asked"
        second = run_tick(settings=_open_settings(), seams=_seams())
        assert second.action == "waiting"
        assert second.reason == "awaiting_human_revert"


class TestDirectiveSpawnedTicketsDoNotCollide(TestCase):
    """The full real tick's two spawned tickets keep distinct namespaced keys (#102).

    The FULL real tick spawns an interpret ticket AND an impl ticket under one
    umbrella (#3009). Both anchor on the same umbrella URL with only a URL fragment
    to disambiguate (``#directive=<pk>`` vs ``#directive-impl=<pk>``), so their
    ``repo_namespaced_key`` must stay distinct — the whole full-tick path throws an
    IntegrityError on the ``unique_nonempty_repo_namespaced_key`` constraint otherwise
    (the PR-8 dogfood collision that blocks enabling ``directive_loop_enabled``).
    """

    def _drive_captured_to_implementing(self) -> Directive:
        directive = Directive.objects.capture("max 1 MR", source=Directive.Source.CLI, scope_overlay=_SCOPE)
        # CAPTURED → interpret_dispatched: creates the synthetic interpret ticket under #3009.
        assert _tick().action == "interpret_dispatched"
        # The recorder binds the sketch (INTERPRETED).
        directive.refresh_from_db()
        directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="c")
        # INTERPRETED → ratify_asked: records the human-approval question.
        assert _tick().action == "ratify_asked"
        directive.refresh_from_db()
        DeferredQuestion.consume(directive.ratify_question_id, answer="approve")
        # RATIFY_PENDING → admitted.
        assert _tick().action == "admitted"
        # ADMITTED → implementing: creates the impl ticket under the SAME #3009 umbrella.
        assert _tick().action == "implementing"
        directive.refresh_from_db()
        return directive

    def test_full_tick_spawns_both_tickets_without_key_collision(self) -> None:
        directive = self._drive_captured_to_implementing()
        interpret_url = f"https://github.com/souliane/teatree/issues/3009#directive={directive.pk}"
        impl_url = f"https://github.com/souliane/teatree/issues/3009#directive-impl={directive.pk}"
        interpret_ticket = Ticket.objects.get(issue_url=interpret_url)
        impl_ticket = Ticket.objects.get(issue_url=impl_url)
        # Both spawned tickets exist and carry DISTINCT, non-empty namespaced keys.
        assert interpret_ticket.repo_namespaced_key
        assert impl_ticket.repo_namespaced_key
        assert interpret_ticket.repo_namespaced_key != impl_ticket.repo_namespaced_key


class TestIdle(TestCase):
    def test_no_active_directive_is_idle(self) -> None:
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "idle"
        assert result.reason == "no_active_directive"
