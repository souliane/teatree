"""Tests for ``teatree.loop.phases.act`` — dispatch + mechanical + persist."""

import datetime as dt
from unittest import mock

import django.test
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


class TestSweepSkipLedgerWiring(django.test.TestCase):
    """The tick's act phase is where a sweep skip becomes a durable streak."""

    def test_act_phase_folds_sweep_skips_into_the_streak_ledger(self) -> None:
        from teatree.core.models import SweepSkipStreak  # noqa: PLC0415 — deferred: ORM needs the app registry

        report = _report(
            [
                ScanSignal(
                    kind="pr_sweep.skip",
                    summary="o/r#7 skip (ci_pending)",
                    payload={"slug": "o/r", "pr_id": 7, "decision": "skip", "reason": "ci_pending"},
                ),
            ],
        )
        act_phase(report)

        assert SweepSkipStreak.objects.get(slug="o/r", pr_id=7).tick_count == 1


class TestRedSetWiring(django.test.TestCase):
    """The tick's act phase is where per-PR sweep signals become ONE set-level claim."""

    @staticmethod
    def _red(pr_id: int, failing: str) -> ScanSignal:
        return ScanSignal(
            kind="pr_sweep.skip",
            summary=f"o/r#{pr_id} skip (ci_red)",
            payload={
                "slug": "o/r",
                "pr_id": pr_id,
                "decision": "skip",
                "reason": "ci_red",
                "overlay": "t3-teatree",
                "url": f"https://github.com/o/r/pull/{pr_id}",
                "failing_required": [failing],
                "base_current": True,
            },
        )

    def test_act_phase_announces_a_board_inheriting_mains_red(self) -> None:
        announced: list[str] = []

        with (
            mock.patch("teatree.loop.red_set_surface._default_main_checks", return_value=frozenset({"shard-a"})),
            mock.patch(
                "teatree.loop.red_set_surface._default_notify",
                side_effect=lambda **kwargs: announced.append(str(kwargs["text"])),
            ),
        ):
            act_phase(_report([self._red(7, "shard-a"), self._red(8, "shard-a")]))

        assert len(announced) == 1
        assert "main-red" in announced[0]

    def test_act_phase_says_nothing_for_a_disjoint_board_on_a_green_main(self) -> None:
        announced: list[str] = []

        with (
            mock.patch("teatree.loop.red_set_surface._default_main_checks", return_value=frozenset()),
            mock.patch(
                "teatree.loop.red_set_surface._default_notify",
                side_effect=lambda **kwargs: announced.append(str(kwargs["text"])),
            ),
        ):
            act_phase(_report([self._red(7, "shard-a"), self._red(8, "shard-b")]))

        assert announced == []

    def test_act_phase_says_nothing_when_the_reds_share_a_cause(self) -> None:
        announced: list[str] = []

        with (
            mock.patch("teatree.loop.red_set_surface._default_main_checks", return_value=frozenset()),
            mock.patch(
                "teatree.loop.red_set_surface._default_notify",
                side_effect=lambda **kwargs: announced.append(str(kwargs["text"])),
            ),
        ):
            act_phase(_report([self._red(7, "shard-a"), self._red(8, "shard-a")]))

        assert announced == []
