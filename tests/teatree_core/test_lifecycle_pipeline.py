"""The pure golden-path sequencer behind ``t3 do`` (PR-31).

These pin the resumability contract (``Ticket.state`` -> per-step status), the
``--plan`` dry-run shape, and the ``drive`` walk against injected seams — no DB,
no ``call_command``, no git.
"""

from itertools import starmap

from teatree.core.lifecycle_pipeline import (
    PIPELINE,
    DoReport,
    DriveSeams,
    LifecycleStep,
    StepKind,
    StepReport,
    StepStatus,
    TicketSnapshot,
    drive,
    resolve_plan,
)
from teatree.core.models import Ticket


def _snapshot(state: str | None, *, provisioned: bool = False) -> TicketSnapshot:
    return TicketSnapshot(
        exists=state is not None,
        state=state,
        provisioned=provisioned,
        ignored=state == Ticket.State.IGNORED,
    )


def _status_by_name(reports: list[StepReport]) -> dict[str, StepStatus]:
    return {r.step.name: r.status for r in reports}


class TestPipelineShape:
    def test_pipeline_is_the_seven_lifecycle_steps_in_order(self) -> None:
        assert [s.name for s in PIPELINE] == [
            "intake",
            "provision",
            "plan",
            "code",
            "test",
            "review",
            "ship",
        ]

    def test_intake_provision_and_ship_are_auto_the_rest_are_agent(self) -> None:
        kinds = {s.name: s.kind for s in PIPELINE}
        assert kinds["intake"] is StepKind.AUTO
        assert kinds["provision"] is StepKind.AUTO
        assert kinds["ship"] is StepKind.AUTO
        assert kinds["plan"] is StepKind.AGENT
        assert kinds["code"] is StepKind.AGENT
        assert kinds["test"] is StepKind.AGENT
        assert kinds["review"] is StepKind.AGENT


class TestResolvePlan:
    def test_absent_ticket_runs_intake_rest_waiting(self) -> None:
        plan = _status_by_name(list(starmap(StepReport, resolve_plan(_snapshot(None)))))
        assert plan["intake"] is StepStatus.RUN
        assert plan["provision"] is StepStatus.WAITING
        assert plan["plan"] is StepStatus.WAITING
        assert plan["ship"] is StepStatus.WAITING

    def test_not_started_runs_intake(self) -> None:
        plan = _status_by_name(list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.NOT_STARTED)))))
        assert plan["intake"] is StepStatus.RUN

    def test_started_unprovisioned_runs_provision(self) -> None:
        plan = _status_by_name(
            list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.STARTED, provisioned=False))))
        )
        assert plan["intake"] is StepStatus.DONE
        assert plan["provision"] is StepStatus.RUN
        assert plan["plan"] is StepStatus.WAITING

    def test_started_provisioned_is_pending_on_the_plan_agent(self) -> None:
        plan = _status_by_name(
            list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.STARTED, provisioned=True))))
        )
        assert plan["intake"] is StepStatus.DONE
        assert plan["provision"] is StepStatus.DONE
        assert plan["plan"] is StepStatus.PENDING
        assert plan["code"] is StepStatus.WAITING

    def test_coded_skips_earlier_agent_phases_and_is_pending_on_test(self) -> None:
        plan = _status_by_name(list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.CODED)))))
        assert plan["plan"] is StepStatus.DONE
        assert plan["code"] is StepStatus.DONE
        assert plan["test"] is StepStatus.PENDING
        assert plan["review"] is StepStatus.WAITING
        assert plan["ship"] is StepStatus.WAITING

    def test_reviewed_runs_ship(self) -> None:
        plan = _status_by_name(list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.REVIEWED)))))
        assert plan["review"] is StepStatus.DONE
        assert plan["ship"] is StepStatus.RUN

    def test_shipped_is_all_done(self) -> None:
        plan = _status_by_name(list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.SHIPPED)))))
        assert all(status is StepStatus.DONE for status in plan.values())

    def test_merged_is_all_done(self) -> None:
        plan = _status_by_name(list(starmap(StepReport, resolve_plan(_snapshot(Ticket.State.MERGED)))))
        assert all(status is StepStatus.DONE for status in plan.values())


class _FakeWorld:
    """A mutable snapshot the fake chokepoint runner advances (no DB/git)."""

    def __init__(self, snapshot: TicketSnapshot, *, ticket_id: int | None = 7) -> None:
        self.snapshot = snapshot
        self.ticket_id = ticket_id
        self.calls: list[str] = []
        self._transitions: dict[str, TicketSnapshot] = {}
        self._details: dict[str, str] = {}

    def when(self, step_name: str, *, becomes: TicketSnapshot | None = None, detail: str = "") -> "_FakeWorld":
        if becomes is not None:
            self._transitions[step_name] = becomes
        if detail:
            self._details[step_name] = detail
        return self

    def run(self, step: LifecycleStep) -> str:
        self.calls.append(step.name)
        if step.name in self._transitions:
            self.snapshot = self._transitions[step.name]
        return self._details.get(step.name, "")

    def drive(self, *, plan_only: bool = False) -> DoReport:
        seams = DriveSeams(
            snapshot_provider=lambda: self.snapshot,
            ticket_id_provider=lambda: self.ticket_id,
            chokepoint_runner=self.run,
        )
        return drive("466", seams, plan_only=plan_only)


class TestDrive:
    def test_fresh_ticket_runs_intake_then_stops_pending_at_plan(self) -> None:
        world = _FakeWorld(_snapshot(None), ticket_id=None).when(
            "intake",
            becomes=_snapshot(Ticket.State.STARTED, provisioned=True),
        )
        report = world.drive()
        statuses = _status_by_name(report.steps)
        assert statuses["intake"] is StepStatus.RAN
        assert statuses["provision"] is StepStatus.DONE
        assert statuses["plan"] is StepStatus.PENDING
        assert statuses["ship"] is StepStatus.WAITING
        assert report.stopped_at == "plan"
        assert report.stopped_reason == "pending"
        assert world.calls == ["intake"]  # never invokes an agent step's chokepoint

    def test_started_unprovisioned_runs_provision_then_stops_pending(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.STARTED, provisioned=False)).when(
            "provision",
            becomes=_snapshot(Ticket.State.STARTED, provisioned=True),
        )
        report = world.drive()
        statuses = _status_by_name(report.steps)
        assert statuses["intake"] is StepStatus.DONE
        assert statuses["provision"] is StepStatus.RAN
        assert statuses["plan"] is StepStatus.PENDING
        assert world.calls == ["provision"]

    def test_reviewed_ships_and_completes(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.REVIEWED)).when(
            "ship",
            becomes=_snapshot(Ticket.State.IN_REVIEW),
        )
        report = world.drive()
        statuses = _status_by_name(report.steps)
        assert statuses["ship"] is StepStatus.RAN
        assert report.stopped_at is None
        assert report.stopped_reason == "completed"
        assert report.final_state == Ticket.State.IN_REVIEW

    def test_ship_that_does_not_advance_is_blocked_with_the_detail(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.REVIEWED)).when(
            "ship",
            detail="Refusing the 'shipping' transition — uncommitted tracked changes",
        )
        report = world.drive()
        ship = next(r for r in report.steps if r.step.name == "ship")
        assert ship.status is StepStatus.BLOCKED
        assert "uncommitted tracked changes" in ship.detail
        assert report.stopped_at == "ship"
        assert report.stopped_reason == "blocked"

    def test_ship_that_does_not_advance_and_is_silent_gets_a_default_blocker(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.REVIEWED))  # ship runs, snapshot unchanged, no detail
        report = world.drive()
        ship = next(r for r in report.steps if r.step.name == "ship")
        assert ship.status is StepStatus.BLOCKED
        assert "did not advance" in ship.detail

    def test_already_shipped_is_idempotent_no_chokepoint_calls(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.MERGED))
        report = world.drive()
        assert all(r.status is StepStatus.DONE for r in report.steps)
        assert report.stopped_reason == "completed"
        assert world.calls == []

    def test_ignored_ticket_stops_without_running_anything(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.IGNORED))
        report = world.drive()
        assert report.stopped_reason == "ignored"
        assert world.calls == []

    def test_plan_only_never_calls_the_chokepoint_runner(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.REVIEWED))
        report = world.drive(plan_only=True)
        assert report.plan_only is True
        assert world.calls == []
        ship = next(r for r in report.steps if r.step.name == "ship")
        assert ship.status is StepStatus.RUN
        assert report.stopped_at == "ship"
        assert report.stopped_reason == "runnable"

    def test_plan_only_on_fresh_ticket_reports_runnable_intake(self) -> None:
        world = _FakeWorld(_snapshot(None), ticket_id=None)
        report = world.drive(plan_only=True)
        assert report.stopped_at == "intake"
        assert report.stopped_reason == "runnable"
        assert world.calls == []

    def test_plan_only_on_completed_ticket_reports_completed(self) -> None:
        world = _FakeWorld(_snapshot(Ticket.State.MERGED))
        report = world.drive(plan_only=True)
        assert report.stopped_at is None
        assert report.stopped_reason == "completed"
        assert all(r.status is StepStatus.DONE for r in report.steps)
