"""``t3 <overlay> do <ticket-ref>`` — the golden-path lifecycle wrapper (PR-31).

``do`` sequences the EXISTING lifecycle chokepoints and reports; it is resumable
(reads ``Ticket.state``), idempotent, surfaces a gate block and stops, and emits
per-step typed status under ``--json``. These pin that contract. The heavy auto
chokepoints (``workspace ticket`` / ``provision`` / ``pr create``) are stubbed at
the ``do._invoke_chokepoint`` seam so the test exercises ``do``'s orchestration —
not the git/docker internals those commands own and test themselves.
"""

import io
import json
from collections.abc import Callable
from typing import cast
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.lifecycle_pipeline import PIPELINE, LifecycleStep
from teatree.core.management.commands import do as do_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.models.errors import DirtyWorktreeError

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_SEAM = "teatree.core.management.commands.do._invoke_chokepoint"


def _ticket(state: str, *, issue_url: str = "") -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=state, issue_url=issue_url)


def _run(*args: str) -> dict[str, object]:
    """Call ``do`` with ``--json`` and return the parsed stdout payload."""
    out, err = io.StringIO(), io.StringIO()
    call_command("do", *args, "--json", stdout=out, stderr=err)
    return json.loads(out.getvalue())


def _steps(payload: dict[str, object]) -> list[dict[str, str]]:
    return cast("list[dict[str, str]]", payload["steps"])


def _status_by_name(payload: dict[str, object]) -> dict[str, str]:
    return {s["name"]: s["status"] for s in _steps(payload)}


class DoPlanDryRunTest(TestCase):
    def test_plan_lists_all_seven_steps_without_side_effects(self) -> None:
        ticket = _ticket(Ticket.State.STARTED)
        payload = _run(str(ticket.pk), "--plan")
        assert payload["plan_only"] is True
        names = [s["name"] for s in _steps(payload)]
        assert names == ["intake", "provision", "plan", "code", "test", "review", "ship"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not Worktree.objects.filter(ticket=ticket).exists()

    def test_plan_reports_runnable_current_step_for_reviewed_ticket(self) -> None:
        ticket = _ticket(Ticket.State.REVIEWED)
        payload = _run(str(ticket.pk), "--plan")
        assert payload["stopped_at"] == "ship"
        assert payload["stopped_reason"] == "runnable"
        assert _status_by_name(payload)["ship"] == "run"

    def test_plan_reports_pending_when_next_step_is_an_agent_phase(self) -> None:
        ticket = _ticket(Ticket.State.PLANNED)
        payload = _run(str(ticket.pk), "--plan")
        assert payload["stopped_at"] == "code"
        assert payload["stopped_reason"] == "pending"

    def test_plan_json_is_pure_on_stdout(self) -> None:
        ticket = _ticket(Ticket.State.STARTED)
        out, err = io.StringIO(), io.StringIO()
        call_command("do", str(ticket.pk), "--plan", "--json", stdout=out, stderr=err)
        json.loads(out.getvalue())  # stdout is exactly one parseable document
        assert out.getvalue().strip().startswith("{")
        assert err.getvalue() == ""  # --json emits no human bytes anywhere

    def test_plan_never_invokes_a_chokepoint(self) -> None:
        ticket = _ticket(Ticket.State.REVIEWED)
        with mock.patch(_SEAM) as seam:
            _run(str(ticket.pk), "--plan")
        seam.assert_not_called()


def _advancing_seam(target_state: str, *, provision: bool = False) -> Callable[..., str]:
    """A stub chokepoint that advances the resolved ticket to *target_state*."""

    def _seam(step: LifecycleStep, *, ref: str, ticket_id: int | None, err: object) -> str:
        if ticket_id is not None:
            ticket = Ticket.objects.get(pk=ticket_id)
        else:
            # intake on an absent ref: the real `workspace ticket` creates the row.
            try:
                ticket = Ticket.objects.resolve(ref)
            except Ticket.DoesNotExist:
                ticket = Ticket.objects.create(overlay="t3-teatree", issue_url=ref)
        ticket.state = target_state
        ticket.save(update_fields=["state"])
        if provision:
            Worktree.objects.get_or_create(
                ticket=ticket,
                overlay="t3-teatree",
                repo_path="souliane/teatree",
                defaults={"branch": "feat/do", "state": Worktree.State.PROVISIONED},
            )
        return ""

    return _seam


class DoRealRunTest(TestCase):
    def test_reviewed_ticket_ships_and_completes(self) -> None:
        ticket = _ticket(Ticket.State.REVIEWED)
        with mock.patch(_SEAM, _advancing_seam(Ticket.State.IN_REVIEW)):
            payload = _run(str(ticket.pk))
        assert _status_by_name(payload)["ship"] == "ran"
        assert payload["stopped_reason"] == "completed"
        assert payload["final_state"] == Ticket.State.IN_REVIEW

    def test_started_provisioned_stops_pending_on_plan_agent(self) -> None:
        ticket = _ticket(Ticket.State.STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo_path="souliane/teatree",
            branch="feat/do",
            state=Worktree.State.PROVISIONED,
        )
        with mock.patch(_SEAM) as seam:
            payload = _run(str(ticket.pk))
        seam.assert_not_called()  # an agent phase is never auto-run
        statuses = _status_by_name(payload)
        assert statuses["intake"] == "done"
        assert statuses["provision"] == "done"
        assert statuses["plan"] == "pending"
        assert payload["stopped_reason"] == "pending"

    def test_coded_ticket_resumes_and_skips_completed_phases(self) -> None:
        ticket = _ticket(Ticket.State.CODED)
        payload = _run(str(ticket.pk))
        statuses = _status_by_name(payload)
        assert statuses["plan"] == "done"
        assert statuses["code"] == "done"
        assert statuses["test"] == "pending"
        assert payload["stopped_at"] == "test"

    def test_absent_ref_runs_intake_then_stops_pending(self) -> None:
        with mock.patch(_SEAM, _advancing_seam(Ticket.State.STARTED, provision=True)):
            payload = _run("https://github.com/souliane/teatree/issues/9999")
        statuses = _status_by_name(payload)
        assert statuses["intake"] == "ran"
        assert statuses["provision"] == "done"
        assert statuses["plan"] == "pending"


class DoBlockedAndTerminalTest(TestCase):
    def test_blocked_ship_exits_nonzero_and_surfaces_the_blocker(self) -> None:
        ticket = _ticket(Ticket.State.REVIEWED)

        def _refusing_seam(step: LifecycleStep, **_: object) -> str:
            return "Refusing the 'shipping' transition — uncommitted tracked changes"

        out, err = io.StringIO(), io.StringIO()
        with mock.patch(_SEAM, _refusing_seam), pytest.raises(SystemExit) as exc:
            call_command("do", str(ticket.pk), "--json", stdout=out, stderr=err)
        assert exc.value.code == 1
        payload = json.loads(out.getvalue())
        ship = next(s for s in _steps(payload) if s["name"] == "ship")
        assert ship["status"] == "blocked"
        assert "uncommitted tracked changes" in ship["blocker"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_ignored_ticket_exits_nonzero(self) -> None:
        ticket = _ticket(Ticket.State.IGNORED)
        out, err = io.StringIO(), io.StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("do", str(ticket.pk), "--json", stdout=out, stderr=err)
        assert exc.value.code == 1
        payload = json.loads(out.getvalue())
        assert payload["stopped_reason"] == "ignored"

    def test_already_shipped_is_idempotent_all_done(self) -> None:
        ticket = _ticket(Ticket.State.MERGED)
        with mock.patch(_SEAM) as seam:
            payload = _run(str(ticket.pk))
        seam.assert_not_called()
        assert all(s["status"] == "done" for s in _steps(payload))
        assert payload["stopped_reason"] == "completed"

    def test_human_mode_keeps_stdout_clean(self) -> None:
        ticket = _ticket(Ticket.State.PLANNED)
        out, err = io.StringIO(), io.StringIO()
        call_command("do", str(ticket.pk), stdout=out, stderr=err)
        assert out.getvalue() == ""  # no --json: nothing on stdout
        assert "plan" in err.getvalue().lower()

    def test_human_mode_renders_the_blocker_line(self) -> None:
        ticket = _ticket(Ticket.State.REVIEWED)

        def _refusing_seam(step: LifecycleStep, **_: object) -> str:
            return "dirty worktree — commit first"

        out, err = io.StringIO(), io.StringIO()
        with mock.patch(_SEAM, _refusing_seam), pytest.raises(SystemExit):
            call_command("do", str(ticket.pk), stdout=out, stderr=err)
        assert "dirty worktree — commit first" in err.getvalue()


def _ship_step() -> LifecycleStep:
    return next(step for step in PIPELINE if step.name == "ship")


class DoChokepointSeamTest(TestCase):
    """The real chokepoint seam — argv mapping + error normalisation (un-mocked)."""

    def test_chokepoint_argv_maps_each_auto_step(self) -> None:
        assert do_mod._chokepoint_argv(_step("intake"), ref="42", ticket_id=None) == ("workspace", "ticket", "42")
        assert do_mod._chokepoint_argv(_step("provision"), ref="42", ticket_id=7) == ("workspace", "provision", "7")
        assert do_mod._chokepoint_argv(_step("ship"), ref="42", ticket_id=7) == ("pr", "create", "7")

    def test_chokepoint_argv_refuses_an_agent_step(self) -> None:
        with pytest.raises(ValueError, match="no chokepoint mapping"):
            do_mod._chokepoint_argv(_step("plan"), ref="42", ticket_id=7)

    def test_result_error_detail_reads_a_typed_error_mapping(self) -> None:
        assert do_mod._result_error_detail({"error": "gate refused"}) == "gate refused"
        assert do_mod._result_error_detail({"ok": True}) == ""
        assert do_mod._result_error_detail("not-a-mapping") == ""

    def test_invoke_chokepoint_normalises_a_returned_error_mapping(self) -> None:
        with mock.patch.object(do_mod, "call_command", return_value={"error": "no commits ahead"}):
            detail = do_mod._invoke_chokepoint(_ship_step(), ref="42", ticket_id=7, err=io.StringIO())
        assert detail == "no commits ahead"

    def test_invoke_chokepoint_normalises_a_string_systemexit(self) -> None:
        with mock.patch.object(do_mod, "call_command", side_effect=SystemExit("  Stopped: db down")):
            detail = do_mod._invoke_chokepoint(_ship_step(), ref="42", ticket_id=7, err=io.StringIO())
        assert detail == "  Stopped: db down"

    def test_invoke_chokepoint_normalises_a_numeric_systemexit(self) -> None:
        with mock.patch.object(do_mod, "call_command", side_effect=SystemExit(1)):
            detail = do_mod._invoke_chokepoint(_ship_step(), ref="42", ticket_id=7, err=io.StringIO())
        assert detail == "the ship chokepoint refused"

    def test_invoke_chokepoint_normalises_an_fsm_refusal(self) -> None:
        with mock.patch.object(do_mod, "call_command", side_effect=DirtyWorktreeError("uncommitted changes")):
            detail = do_mod._invoke_chokepoint(_ship_step(), ref="42", ticket_id=7, err=io.StringIO())
        assert detail == "uncommitted changes"

    def test_invoke_chokepoint_clean_run_has_no_detail(self) -> None:
        with mock.patch.object(do_mod, "call_command", return_value=7):
            detail = do_mod._invoke_chokepoint(_step("intake"), ref="42", ticket_id=None, err=io.StringIO())
        assert detail == ""


def _step(name: str) -> LifecycleStep:
    return next(step for step in PIPELINE if step.name == name)
