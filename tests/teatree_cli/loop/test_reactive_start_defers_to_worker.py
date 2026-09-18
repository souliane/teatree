"""`t3 loop <slot> start` must not hand out a `/loop` a live worker already drives (#2663).

The three reactive slots run as worker chains. Printing a paste-me slash command
while the worker holds the singleton is what produced hand-run loop crons whose
every tick was a guaranteed no-op.
"""

import pytest
from typer.testing import CliRunner

from teatree.cli.loop import reactive_start
from teatree.cli.loop.app import loop_app

_PROBE_FAILURE = RuntimeError("probe unavailable")

_SLOTS = ["slack-answer", "self-improve", "drain-queue"]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestWorkerAlive:
    @pytest.mark.parametrize("slot", _SLOTS)
    def test_start_prints_no_loop_directive(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, slot: str
    ) -> None:
        monkeypatch.setattr(reactive_start, "_worker_is_running", lambda: True)

        result = runner.invoke(loop_app, [slot, "start"])

        assert result.exit_code == 0, result.output
        assert "/loop " not in result.output
        assert "t3 worker status" in result.output


class TestWorkerDown:
    @pytest.mark.parametrize("slot", _SLOTS)
    def test_start_still_prints_the_registration(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, slot: str
    ) -> None:
        monkeypatch.setattr(reactive_start, "_worker_is_running", lambda: False)

        result = runner.invoke(loop_app, [slot, "start"])

        assert result.exit_code == 0, result.output
        assert "/loop " in result.output


class TestProbeFailure:
    def test_unprovable_worker_falls_back_to_printing_the_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> bool:
            raise _PROBE_FAILURE

        monkeypatch.setattr(reactive_start, "_a_worker_is_running", _boom)

        assert reactive_start._worker_is_running() is False
