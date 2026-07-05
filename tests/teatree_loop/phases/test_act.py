"""Tests for ``teatree.loop.phases.act`` — dispatch + mechanical + persist."""

import datetime as dt

import pytest

from teatree.loop.phases.act import act_phase
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport


def _report(signals: list[ScanSignal]) -> TickReport:
    return TickReport(started_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC), signals=signals)


def test_act_phase_dispatches_signals_into_actions() -> None:
    report = _report([ScanSignal(kind="my_pr.open", summary="PR open")])
    act_phase(report)
    assert any(a.kind == "statusline" for a in report.actions)


def test_act_phase_runs_mechanical_handler_and_captures_its_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from teatree.loop import mechanical  # noqa: PLC0415

    def boom(_payload: dict) -> None:
        msg = "handler exploded"
        raise RuntimeError(msg)

    monkeypatch.setitem(mechanical.HANDLERS, "ticket_completion", boom)
    report = _report(
        [ScanSignal(kind="ticket.completion_detected", summary="ready", payload={"ticket_id": 42})],
    )
    act_phase(report)
    assert any("handler exploded" in msg for msg in report.errors.values())


def test_act_phase_captures_persist_failure_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_actions: object, *, errors: object = None) -> None:
        msg = "persistence down"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.loop.persistence.persist_agent_actions", boom)
    report = _report([ScanSignal(kind="my_pr.open", summary="x")])
    act_phase(report)
    assert "persistence down" in report.errors["dispatch_persist"]
