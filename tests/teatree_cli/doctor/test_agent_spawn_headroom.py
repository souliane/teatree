"""The spawn-headroom gauge — a cliff reported as a trend (#4301)."""

from unittest.mock import patch

import pytest

import teatree.cli.doctor.checks_agent_spawn as module
from teatree.agents.spawn_payload import SpawnPayload
from teatree.cli.doctor.checks_agent_spawn import _check_agent_spawn_headroom


def _payload(*, argv_bytes: int, limit: int = 2_097_152) -> SpawnPayload:
    return SpawnPayload(argv_bytes=argv_bytes, env_bytes=0, largest_arg_bytes=64, total_limit_bytes=limit)


class TestCheckAgentSpawnHeadroom:
    def test_a_healthy_box_is_silent_and_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(module, "preflight_payload", return_value=_payload(argv_bytes=10_000)):
            assert _check_agent_spawn_headroom()
        assert capsys.readouterr().out == ""

    def test_approaching_the_limit_warns_without_gating(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(module, "preflight_payload", return_value=_payload(argv_bytes=1_800_000)):
            assert _check_agent_spawn_headroom()
        assert "WARN" in capsys.readouterr().out

    def test_a_floor_over_the_limit_hard_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Every dispatch on such a box dies at execve before doing any work.
        with patch.object(module, "preflight_payload", return_value=_payload(argv_bytes=3_000_000)):
            assert not _check_agent_spawn_headroom()
        output = capsys.readouterr().out
        assert "FAIL" in output
        assert "E2BIG" in output

    def test_a_probe_error_degrades_to_a_silent_pass(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(module, "preflight_payload", side_effect=OSError("no /proc")):
            assert _check_agent_spawn_headroom()
        assert capsys.readouterr().out == ""

    def test_the_real_probe_runs_on_this_host(self) -> None:
        # No stub: the gauge must survive a real measurement, which is the whole point.
        assert _check_agent_spawn_headroom() in {True, False}
