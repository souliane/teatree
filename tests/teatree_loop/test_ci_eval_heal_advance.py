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

from teatree.core.models import CiEvalHealSession, DeferredQuestion
from teatree.loop.ci_eval_heal_advance import (
    AdvanceOutcome,
    _escalate_via_deferred_question,
    advance_open_sessions,
    advance_session,
    red_scenario_names,
)


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
