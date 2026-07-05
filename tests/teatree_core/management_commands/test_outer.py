"""``t3 outer`` operator verbs — status / history / propose / tick (T4-PR-3).

Pins the inert-at-defaults contract: ``status`` reports the guard chain REFUSING
(outer_loop_disabled), ``propose`` REFUSES while the flag is off (nothing is ever
created at defaults), and ``tick`` SKIPs while the seeded Loop row is disabled.
With the flag on, ``propose`` records an operator hypothesis.
"""

from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import (
    DeferredQuestion,
    FactoryScoreSnapshot,
    Loop,
    LoopLease,
    OuterLoopExperiment,
    ProposalSpec,
    Ticket,
)
from teatree.loops.outer_loop.tick import OuterLoopTickResult


def _run(*args: str) -> str:
    out = StringIO()
    call_command("outer", *args, stdout=out)
    return out.getvalue()


def _revert_pending() -> OuterLoopExperiment:
    exp = OuterLoopExperiment.objects.propose(
        ProposalSpec(hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR)
    )
    answered = DeferredQuestion.record("Ratify?")
    DeferredQuestion.consume(answered.pk, answer="approve")
    exp.attach_ratification(DeferredQuestion.objects.get(pk=answered.pk))
    exp.admit()
    exp.begin_implementation(
        Ticket.objects.create(issue_url=f"https://e.com/{timezone.now().timestamp()}", role=Ticket.Role.AUTHOR)
    )
    exp.arm_measure()
    exp.request_revert(
        post_snapshot=FactoryScoreSnapshot.objects.create(
            overlay="", window_days=7, recipe_sha="s", aggregate=0.6, verdict="ok", coverage=1.0, coverage_floor=0.6
        ),
        reason="no improvement",
    )
    return exp


class TestOuterCommandInertAtDefaults(TestCase):
    def test_status_reports_the_guard_chain_refusing(self) -> None:
        output = _run("status")
        assert "REFUSE" in output
        assert "outer_loop_disabled" in output
        assert "no active experiment" in output

    def test_history_is_empty(self) -> None:
        assert "no experiments recorded" in _run("history")

    def test_propose_refuses_while_the_flag_is_off(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("outer", "propose", hypothesis="Try X", target="review_catch")
        assert exc.value.code == 2
        assert OuterLoopExperiment.objects.count() == 0

    def test_tick_skips_while_the_loop_row_is_disabled(self) -> None:
        _seed_outer_loop_row(enabled=False)
        assert "SKIP" in _run("tick")
        assert OuterLoopExperiment.objects.count() == 0


class TestOuterTickWhenEnabled(TestCase):
    def setUp(self) -> None:
        call_command("config_setting", "set", "outer_loop_enabled", "true")
        call_command("config_setting", "set", "factory_score_enabled", "true")
        _seed_outer_loop_row(enabled=True)

    def test_tick_runs_the_guard_chain_and_refuses_without_a_live_critic(self) -> None:
        # Flag + loop row on, but the critic code guard fails closed → the tick
        # runs, refuses, marks the run, and creates nothing (the shipped state).
        output = _run("tick")
        assert "outer_loop tick" in output
        assert "critic_not_live" in output
        assert OuterLoopExperiment.objects.count() == 0

    def test_tick_skips_when_the_cadence_has_not_elapsed(self) -> None:
        Loop.objects.filter(name="outer_loop").update(last_run_at=timezone.now())
        assert "cadence not elapsed" in _run("tick")

    def test_tick_skips_when_the_lease_is_held(self) -> None:
        with mock.patch.object(LoopLease.objects, "acquire", return_value=False):
            assert "lease held" in _run("tick")

    def test_tick_ok_line_names_the_experiment(self) -> None:
        # When the guard chain passes and a proposal is created, the OK line names
        # the experiment id (patched run_tick so the display path is exercised).
        result = OuterLoopTickResult(action="proposed", experiment_id=42)
        with mock.patch("teatree.loops.outer_loop.tick.run_tick", return_value=result):
            output = _run("tick")
        assert "proposed" in output
        assert "experiment=42" in output


class TestOuterResolveRevert(TestCase):
    def test_missing_experiment_errors(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("outer", "resolve-revert", 999)
        assert exc.value.code == 1

    def test_wrong_state_errors(self) -> None:
        exp = OuterLoopExperiment.objects.propose(
            ProposalSpec(hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR)
        )
        with pytest.raises(SystemExit) as exc:
            call_command("outer", "resolve-revert", exp.pk)
        assert exc.value.code == 1

    def test_reverts_a_pending_experiment_and_frees_the_slot(self) -> None:
        exp = _revert_pending()
        output = _run("resolve-revert", str(exp.pk), "--revert-sha", "cafe")
        assert "reverted experiment" in output
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.REVERTED
        assert reloaded.revert_sha == "cafe"
        assert OuterLoopExperiment.objects.active_count() == 0


def _seed_outer_loop_row(*, enabled: bool) -> None:
    # ``update_or_create`` (not ``get_or_create``) so ``enabled`` is FORCED — the
    # migration already seeds the ``outer_loop`` row disabled, against which a
    # ``get_or_create`` defaults block never runs (the enabled=True would no-op).
    Loop.objects.update_or_create(
        name="outer_loop",
        defaults={"delay_seconds": 86400, "enabled": enabled, "script": "src/teatree/loops/outer_loop/loop.py"},
    )


class TestOuterProposeWhenEnabled(TestCase):
    def setUp(self) -> None:
        call_command("config_setting", "set", "outer_loop_enabled", "true")

    def test_propose_records_an_operator_experiment(self) -> None:
        output = _run("propose", "--hypothesis", "Tighten the review gate.", "--target", "review_catch")
        assert "proposed experiment" in output
        exp = OuterLoopExperiment.objects.get()
        assert exp.source == OuterLoopExperiment.Source.OPERATOR
        assert exp.target_provider_id == "review_catch"
        assert exp.state == OuterLoopExperiment.State.PROPOSED

    def test_propose_requires_both_arguments(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("outer", "propose", hypothesis="", target="review_catch")
        assert exc.value.code == 1

    def test_history_lists_the_experiment(self) -> None:
        _run("propose", "--hypothesis", "H", "--target", "merge_latency")
        assert "merge_latency" in _run("history")

    def test_status_shows_the_active_experiment(self) -> None:
        _run("propose", "--hypothesis", "H", "--target", "review_catch")
        output = _run("status")
        assert "active experiment #" in output
        assert "review_catch" in output
