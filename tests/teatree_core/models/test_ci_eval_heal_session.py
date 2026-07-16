"""``CiEvalHealSession`` FSM — the CI-eval self-healing loop's durable state (#3201 PR-2).

One session tracks one PR branch through the heal loop: dispatch a CI eval run,
triage the result, fix behavioral reds, push, re-trigger — until GREEN, or HALT
and escalate when a red cannot be greened. Two invariants are enforced at the
model level:

* ``mark_green`` is refused while any red scenario remains — a red is never
    silently suppressed into a green session.
* the ``record_fix`` transition runs the anti-cheat gate, so a fix diff touching
    the scenario tree or the red matcher rolls the transition back.
"""

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.gates.eval_heal_anticheat_gate import EvalHealCheatError
from teatree.core.models import CiEvalHealSession, CiEvalHealSessionManager


def _session(**kwargs: object) -> CiEvalHealSession:
    defaults: dict[str, object] = {"overlay": "teatree", "pr_ref": "3201-feat-x"}
    defaults.update(kwargs)
    return CiEvalHealSession.objects.create(**defaults)


class TestManager:
    pytestmark = pytest.mark.django_db

    def test_default_manager_is_the_typed_manager(self) -> None:
        assert isinstance(CiEvalHealSession.objects, CiEvalHealSessionManager)

    def test_open_session_starts_pending(self) -> None:
        session = CiEvalHealSession.objects.open_session(overlay="teatree", pr_ref="3201-feat-x")
        assert session.state == CiEvalHealSession.State.PENDING
        assert session.fix_attempts == 0


class TestHappyPathGreen(TestCase):
    def test_trigger_then_green_when_no_reds(self) -> None:
        session = _session()
        session.trigger(ci_run_id="run-1", head_sha="a" * 40)
        session.save()
        assert session.state == CiEvalHealSession.State.AWAITING_CI
        assert session.ci_run_id == "run-1"

        session.receive_result(red_scenarios=[])
        session.save()
        assert session.state == CiEvalHealSession.State.TRIAGING

        session.mark_green()
        session.save()
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.GREEN


class TestNeverSuppressARed(TestCase):
    def test_mark_green_refused_while_reds_remain(self) -> None:
        session = _session()
        session.trigger(ci_run_id="run-1", head_sha="a" * 40)
        session.save()
        session.receive_result(red_scenarios=["rules_under_load"])
        session.save()
        with pytest.raises(TransitionNotAllowed):
            session.mark_green()
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.TRIAGING


class TestFixLoop(TestCase):
    def _to_triage_with_reds(self) -> CiEvalHealSession:
        session = _session(max_fix_attempts=3)
        session.trigger(ci_run_id="run-1", head_sha="a" * 40)
        session.save()
        session.receive_result(red_scenarios=["rules_under_load"])
        session.save()
        return session

    def test_begin_fix_then_record_clean_fix_pushes(self) -> None:
        session = self._to_triage_with_reds()
        session.begin_fix()
        session.save()
        assert session.state == CiEvalHealSession.State.FIXING

        session.record_fix(changed_paths=["skills/rules/SKILL.md"])
        session.save()
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.PUSHED
        assert session.fix_attempts == 1
        assert session.last_fix_paths == ["skills/rules/SKILL.md"]

    def test_record_fix_anti_cheat_rolls_back(self) -> None:
        # The load-bearing gate: a fix that edits the scenario yaml is refused and
        # the FSM stays in FIXING — no push, the red is not suppressed.
        session = self._to_triage_with_reds()
        session.begin_fix()
        session.save()
        with pytest.raises(EvalHealCheatError):
            session.record_fix(changed_paths=["evals/scenarios/rules.yaml"])
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.FIXING
        assert session.fix_attempts == 0

    def test_pushed_re_triggers_next_ci_run(self) -> None:
        session = self._to_triage_with_reds()
        session.begin_fix()
        session.save()
        session.record_fix(changed_paths=["src/teatree/loop/tick.py"])
        session.save()
        session.trigger(ci_run_id="run-2", head_sha="b" * 40)
        session.save()
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.AWAITING_CI
        assert session.ci_run_id == "run-2"


class TestHaltAndEscalate(TestCase):
    def test_halt_from_triaging_records_reason(self) -> None:
        session = _session()
        session.trigger(ci_run_id="run-1", head_sha="a" * 40)
        session.save()
        session.receive_result(red_scenarios=["rules_under_load"])
        session.save()
        session.halt(reason="un-greenable behavioral red after 3 attempts")
        session.save()
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert "un-greenable" in session.halt_reason

    def test_fix_budget_exhausted_property(self) -> None:
        session = _session(max_fix_attempts=1, fix_attempts=1)
        assert session.fix_budget_exhausted is True

    def test_fix_budget_not_exhausted(self) -> None:
        session = _session(max_fix_attempts=3, fix_attempts=1)
        assert session.fix_budget_exhausted is False
