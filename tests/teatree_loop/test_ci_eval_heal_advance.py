"""Observe-only CI-eval heal advancer (#3201 PR-3a).

The loop that drives the ``CiEvalHealSession`` FSM without ever writing a fix. The
load-bearing property, asserted red-first here, is the anti-cheat invariant: a
genuinely-failing eval is NEVER marked green — a red (or an infra failure, or an
unconfirmable result) always terminates at ``HALTED`` and escalates, and ``GREEN``
is reachable only from a CI ``success``.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import CiEvalHealSession, ConfigSetting, DeferredQuestion, Loop
from teatree.loop.ci_eval_heal_advance import (
    AdvanceOutcome,
    _escalate_via_deferred_question,
    advance_open_sessions,
    advance_session,
    red_scenario_names,
)
from teatree.loop.ci_eval_heal_fixer import FixProposal


class _FakeClient:
    """A GhCiEvalClient stand-in that records dispatches and replays canned run/verdict data."""

    def __init__(
        self,
        *,
        head_sha: str = "a" * 40,
        runs: list[dict[str, object]] | None = None,
        artifact: dict[str, object] | None = None,
        download_error: Exception | None = None,
    ) -> None:
        self._head_sha = head_sha
        self._runs = runs if runs is not None else []
        self._artifact = artifact
        self._download_error = download_error
        self.triggered: list[dict[str, object]] = []

    def resolve_head_sha(self, ref: str) -> str:
        return self._head_sha

    def trigger_workflow(self, workflow: str, *, ref: str, inputs: dict[str, str]) -> None:
        self.triggered.append({"workflow": workflow, "ref": ref, "inputs": inputs})

    def list_runs(self, workflow: str, *, branch: str, limit: int = 20) -> list[dict[str, object]]:
        return self._runs

    def download_artifact(self, run_id: int | str, *, name: str, dest_dir: Path) -> None:
        if self._download_error is not None:
            raise self._download_error
        if self._artifact is None:
            return  # a directory with no JSON — the "unconfirmable" path
        (Path(dest_dir) / "eval-heal.json").write_text(json.dumps(self._artifact), encoding="utf-8")


def _completed(conclusion: str, *, head_sha: str = "a" * 40, run_id: int = 77) -> dict[str, object]:
    return {"headSha": head_sha, "status": "completed", "conclusion": conclusion, "databaseId": run_id}


def _artifact(*, reds: Sequence[str], greens: Sequence[str] = ()) -> dict[str, object]:
    scenarios = [{"name": name, "triage_class": "behavioral"} for name in reds]
    scenarios += [{"name": name, "triage_class": None} for name in greens]
    return {"scenarios": scenarios}


def _session(**kwargs: object) -> CiEvalHealSession:
    defaults: dict[str, object] = {"overlay": "teatree", "pr_ref": "3201-feat-x"}
    defaults.update(kwargs)
    return CiEvalHealSession.objects.create(**defaults)


class TestRedScenarioNames:
    def test_reds_are_the_non_null_triage_class_scenarios(self) -> None:
        payload = _artifact(reds=["rules_under_load", "budget_turns"], greens=["ok_one"])
        assert red_scenario_names(payload) == ["rules_under_load", "budget_turns"]

    def test_malformed_payload_yields_no_reds_never_raises(self) -> None:
        assert red_scenario_names({}) == []
        assert red_scenario_names({"scenarios": "not-a-list"}) == []


class TestDispatchStep(TestCase):
    def test_pending_dispatches_full_suite_on_subscription_and_awaits(self) -> None:
        session = _session()
        client = _FakeClient(head_sha="b" * 40)
        outcome = advance_session(session, client=client, escalate=_never)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.AWAITING_CI
        assert session.head_sha == "b" * 40
        assert client.triggered[0]["inputs"] == {
            "scenarios": "",
            "credential": "subscription_oauth",
            "pr_ref": "3201-feat-x",
        }
        assert outcome.to_state == CiEvalHealSession.State.AWAITING_CI


class TestObserveGreenPath(TestCase):
    def test_ci_success_marks_green(self) -> None:
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[_completed("success")])
        advance_session(session, client=client, escalate=_never)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.GREEN

    def test_run_still_in_flight_is_a_noop(self) -> None:
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[{"headSha": "a" * 40, "status": "in_progress", "conclusion": ""}])
        advance_session(session, client=client, escalate=_never)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.AWAITING_CI

    def test_no_matching_run_yet_is_a_noop(self) -> None:
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[_completed("success", head_sha="z" * 40)])
        advance_session(session, client=client, escalate=_never)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.AWAITING_CI


class TestAntiCheatNeverGreensARed(TestCase):
    """The non-negotiable invariant — a genuinely-failing eval can NEVER become GREEN."""

    def test_ci_failure_with_reds_halts_and_escalates_never_green(self) -> None:
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[_completed("failure")], artifact=_artifact(reds=["rules_under_load"]))
        escalated: list[int] = []
        advance_session(session, client=client, escalate=lambda s: escalated.append(s.pk))
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert session.state != CiEvalHealSession.State.GREEN
        assert session.red_scenarios == ["rules_under_load"]
        assert escalated == [session.pk]
        assert "rules_under_load" in session.halt_reason

    def test_triaging_with_reds_never_reaches_green(self) -> None:
        # Drive the FSM straight to TRIAGING carrying a red, then advance: the only
        # legal terminal is HALTED. mark_green must never fire while a red remains.
        session = _awaiting()
        session.receive_result(red_scenarios=["budget_turns"])
        session.save()
        advance_session(session, client=_FakeClient(), escalate=_noop)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED

    def test_infra_failure_without_reds_halts_never_green(self) -> None:
        # A non-success conclusion whose artifact carries NO behavioral red is an
        # infra failure — it must HALT, never collapse to an empty (green) red set.
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[_completed("cancelled")], artifact=_artifact(reds=[]))
        advance_session(session, client=client, escalate=_noop)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert "infra" in session.halt_reason

    def test_failure_with_unfetchable_artifact_halts_never_green(self) -> None:
        # The reds cannot be confirmed (download error) — treat as infra HALT, never
        # a false green. This is the "no silent green" guarantee under a flaky gh.
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[_completed("failure")], download_error=FileNotFoundError("no artifact"))
        advance_session(session, client=client, escalate=_noop)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED


class TestNeverFixes(TestCase):
    def test_observe_loop_never_touches_fix_branch_states(self) -> None:
        # A session parked in FIXING/PUSHED (PR-3b territory) is a no-op for the
        # observe loop — it writes no fix and never advances the fix branch.
        session = _awaiting()
        session.receive_result(red_scenarios=["x"])
        session.save()
        session.begin_fix()
        session.save()
        assert session.state == CiEvalHealSession.State.FIXING
        outcome = advance_session(session, client=_FakeClient(), escalate=_never)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.FIXING
        assert "no-op" in outcome.note


class TestAdvanceOpenSessions(TestCase):
    def test_advances_only_open_sessions_and_swallows_per_session_errors(self) -> None:
        green = _session(pr_ref="already-green")
        green.trigger(ci_run_id="", head_sha="a" * 40)
        green.save()
        green.receive_result(red_scenarios=[])
        green.save()
        green.mark_green()
        green.save()

        pending = _session(pr_ref="to-dispatch")

        client = _FakeClient(head_sha="c" * 40)
        run = advance_open_sessions(client=client, escalate=_never)
        pending.refresh_from_db()
        green.refresh_from_db()
        # The GREEN (terminal) session is untouched; the pending one is dispatched.
        assert green.state == CiEvalHealSession.State.GREEN
        assert pending.state == CiEvalHealSession.State.AWAITING_CI
        assert {o.pr_ref for o in run.outcomes} == {"to-dispatch"}

    def test_returns_outcomes_dataclass(self) -> None:
        run = advance_open_sessions(client=_FakeClient(), escalate=_never)
        assert run.outcomes == []
        assert run.errors == {}


class TestEscalationDefault(TestCase):
    def test_halt_records_deferred_question_once(self) -> None:
        session = _awaiting(head_sha="a" * 40)
        client = _FakeClient(runs=[_completed("failure")], artifact=_artifact(reds=["rules_under_load"]))
        # Use the real default escalation (no escalate= override).
        advance_session(session, client=client, escalate=_escalate_via_deferred_question)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        marker = f"[ci-eval-heal-halt session={session.pk}]"
        assert DeferredQuestion.objects.filter(question__contains=marker).count() == 1
        # Idempotent: escalating the same halted session again creates no duplicate.
        _escalate_via_deferred_question(session)
        assert DeferredQuestion.objects.filter(question__contains=marker).count() == 1


class _FakeFixer:
    """A CiEvalHealFixer spy — proposes canned paths, records publish/discard, never touches git."""

    def __init__(
        self,
        *,
        changed_paths: Sequence[str] = ("src/teatree/skills/t3-rules/SKILL.md",),
        head_sha: str = "f" * 40,
        raise_on_propose: Exception | None = None,
    ) -> None:
        self._changed = tuple(changed_paths)
        self._head = head_sha
        self._raise = raise_on_propose
        self.proposed = 0
        self.published: list[FixProposal] = []
        self.discarded: list[FixProposal] = []

    def propose(self, session: CiEvalHealSession) -> FixProposal:
        self.proposed += 1
        if self._raise is not None:
            raise self._raise
        return FixProposal(changed_paths=self._changed, worktree_path="/tmp/wt", base_sha="base", commit_sha="c0ffee")

    def publish(self, session: CiEvalHealSession, proposal: FixProposal) -> str:
        self.published.append(proposal)
        return self._head

    def discard(self, proposal: FixProposal) -> None:
        self.discarded.append(proposal)


def _arm_autofix() -> None:
    """Turn on BOTH switches — the DARK flag AND the ci_eval_heal loop row."""
    ConfigSetting.objects.set_value("ci_eval_heal_autofix_enabled", value=True)
    Loop.objects.update_or_create(
        name="ci_eval_heal",
        defaults={"enabled": True, "delay_seconds": 300, "script": "src/teatree/loops/ci_eval_heal/loop.py"},
    )


def _red_run() -> "_FakeClient":
    return _FakeClient(runs=[_completed("failure")], artifact=_artifact(reds=["rules_under_load"]))


class TestFixerDisarmedIsObserveOnly(TestCase):
    def test_red_halts_and_never_dispatches_when_disarmed(self) -> None:
        session = _awaiting(head_sha="a" * 40)
        fixer = _FakeFixer()
        advance_session(session, client=_red_run(), escalate=_noop, fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert session.state != CiEvalHealSession.State.GREEN
        assert fixer.proposed == 0
        assert "observe-only" in session.halt_reason

    def test_flag_on_but_loop_off_stays_observe_only(self) -> None:
        ConfigSetting.objects.set_value("ci_eval_heal_autofix_enabled", value=True)  # only ONE switch
        session = _awaiting(head_sha="a" * 40)
        fixer = _FakeFixer()
        advance_session(session, client=_red_run(), escalate=_noop, fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert fixer.proposed == 0


class TestArmedFixerDispatch(TestCase):
    def test_behavioral_red_dispatches_a_bounded_fix_and_retriggers(self) -> None:
        _arm_autofix()
        session = _awaiting(head_sha="a" * 40)
        client = _red_run()
        fixer = _FakeFixer(changed_paths=("src/teatree/skills/t3-rules/SKILL.md",), head_sha="f" * 40)
        advance_session(session, client=client, escalate=_never, fixer=fixer)
        session.refresh_from_db()
        # begin_fix -> propose -> gate -> publish -> re-trigger.
        assert fixer.proposed == 1
        assert len(fixer.published) == 1
        assert session.state == CiEvalHealSession.State.AWAITING_CI
        assert session.fix_attempts == 1
        assert session.last_fix_paths == ["src/teatree/skills/t3-rules/SKILL.md"]
        assert session.head_sha == "f" * 40
        # The re-trigger dispatched a fresh eval on the fixed branch.
        assert client.triggered
        assert client.triggered[-1]["inputs"] == {
            "scenarios": "",
            "credential": "subscription_oauth",
            "pr_ref": "3201-feat-x",
        }

    def test_no_change_proposal_halts_never_green(self) -> None:
        _arm_autofix()
        session = _awaiting(head_sha="a" * 40)
        fixer = _FakeFixer(changed_paths=())
        escalated: list[int] = []
        advance_session(session, client=_red_run(), escalate=lambda s: escalated.append(s.pk), fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert session.state != CiEvalHealSession.State.GREEN
        assert fixer.published == []
        assert len(fixer.discarded) == 1
        assert "no change" in session.halt_reason
        assert escalated == [session.pk]

    def test_dispatch_failure_halts_never_stuck_in_fixing(self) -> None:
        _arm_autofix()
        session = _awaiting(head_sha="a" * 40)
        fixer = _FakeFixer(raise_on_propose=RuntimeError("spawn boom"))
        advance_session(session, client=_red_run(), escalate=_noop, fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert "dispatch failed" in session.halt_reason


class TestAntiCheatRejectsTestEdit(TestCase):
    """The load-bearing PR-3b guardrail — a fixer that edits the TEST is rejected, never pushed."""

    def test_scenario_edit_is_rejected_discarded_and_halts_never_green(self) -> None:
        _arm_autofix()
        session = _awaiting(head_sha="a" * 40)
        fixer = _FakeFixer(changed_paths=("evals/scenarios/rules_under_load.yaml",))
        escalated: list[int] = []
        advance_session(session, client=_red_run(), escalate=lambda s: escalated.append(s.pk), fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert session.state != CiEvalHealSession.State.GREEN
        # The cheating diff never reached the branch and never spent budget.
        assert fixer.published == []
        assert len(fixer.discarded) == 1
        assert session.fix_attempts == 0
        assert "edit the eval test" in session.halt_reason
        assert escalated == [session.pk]

    def test_red_matcher_edit_is_rejected(self) -> None:
        _arm_autofix()
        session = _awaiting(head_sha="a" * 40)
        fixer = _FakeFixer(changed_paths=("src/teatree/eval/matchers.py", "src/teatree/skills/foo.md"))
        advance_session(session, client=_red_run(), escalate=_noop, fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert fixer.published == []
        assert session.fix_attempts == 0


class TestFixBudgetIsBounded(TestCase):
    def test_exhausted_budget_halts_without_dispatch(self) -> None:
        _arm_autofix()
        session = _awaiting(head_sha="a" * 40)
        session.max_fix_attempts = 2
        session.fix_attempts = 2
        session.save()
        fixer = _FakeFixer()
        escalated: list[int] = []
        advance_session(session, client=_red_run(), escalate=lambda s: escalated.append(s.pk), fixer=fixer)
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.HALTED
        assert fixer.proposed == 0
        assert "budget exhausted" in session.halt_reason
        assert escalated == [session.pk]


class TestPushedRetrigger(TestCase):
    def test_pushed_session_retriggers_the_eval_on_the_fixed_branch(self) -> None:
        session = _awaiting()
        session.receive_result(red_scenarios=["rules_under_load"])
        session.save()
        session.begin_fix()
        session.save()
        session.record_fix(changed_paths=["src/teatree/skills/foo.md"])
        session.save()
        assert session.state == CiEvalHealSession.State.PUSHED
        client = _FakeClient(head_sha="n" * 40)
        outcome = advance_session(session, client=client, escalate=_never, fixer=_FakeFixer())
        session.refresh_from_db()
        assert session.state == CiEvalHealSession.State.AWAITING_CI
        assert session.head_sha == "n" * 40
        assert client.triggered
        assert outcome.to_state == CiEvalHealSession.State.AWAITING_CI


class TestRedNeverSelfCertifiesGreen(TestCase):
    """Sweep: under every arming/budget/cheat combination, a red never reaches GREEN."""

    def test_a_red_is_never_green_across_the_fixer_matrix(self) -> None:
        # (arm?, changed_paths) → each must terminate NOT-GREEN while a red is present.
        cases: list[tuple[bool, tuple[str, ...]]] = [
            (False, ("src/teatree/skills/foo.md",)),  # disarmed observe-only
            (True, ()),  # armed but un-fixable
            (True, ("evals/scenarios/x.yaml",)),  # armed but cheating
        ]
        for arm, paths in cases:
            if arm:
                _arm_autofix()
            session = _awaiting(head_sha="a" * 40)
            advance_session(session, client=_red_run(), escalate=_noop, fixer=_FakeFixer(changed_paths=paths))
            session.refresh_from_db()
            assert session.state != CiEvalHealSession.State.GREEN, (arm, paths)
            assert session.state == CiEvalHealSession.State.HALTED, (arm, paths)
            ConfigSetting.objects.clear("ci_eval_heal_autofix_enabled")
            Loop.objects.filter(name="ci_eval_heal").delete()


def _awaiting(*, head_sha: str = "a" * 40) -> CiEvalHealSession:
    session = _session()
    session.trigger(ci_run_id="", head_sha=head_sha)
    session.save()
    return session


def _never(_session: CiEvalHealSession) -> None:
    """An escalation spy that must not fire on the green/no-op paths."""
    pytest.fail("escalation fired on a non-halting path")


def _noop(_session: CiEvalHealSession) -> None:
    """A silent escalation stub for halting paths that don't assert the escalation itself."""


def test_advance_outcome_is_frozen() -> None:
    outcome = AdvanceOutcome("ref", "pending", "awaiting_ci", note="x")
    assert outcome.pr_ref == "ref"
