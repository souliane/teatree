# test-path: cross-cutting — asserts over dev/push-gate.sh; no src/teatree/ mirror.
"""A failing stage fails the push gate; the conformance cap is its one soft exit."""

from pathlib import Path

import pytest

from tests._push_gate_lane import install_uv_stub, lane_repo, run_lane, stage_runs

_STAGE_FAILURE_RC = 3
_TIMEOUT_RC = 124


@pytest.mark.integration
class TestAFailingStageFailsThePushGate:
    @pytest.mark.parametrize("stage", [1, 2, 3])
    def test_the_stage_status_is_the_gate_status_and_no_later_stage_runs(self, tmp_path: Path, stage: int) -> None:
        install_uv_stub(tmp_path)

        completed, _, _ = run_lane(
            tmp_path,
            lane_repo(tmp_path, vendored=False),
            extra_env={"UV_STUB_FAIL_STAGE": str(stage), "UV_STUB_FAIL_RC": str(_STAGE_FAILURE_RC)},
        )

        assert completed.returncode == _STAGE_FAILURE_RC, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert [run.number for run in stage_runs(tmp_path)] == list(range(1, stage + 1))

    def test_a_conformance_core_over_its_cap_is_reported_and_not_a_failure(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)

        completed, _, _ = run_lane(
            tmp_path,
            lane_repo(tmp_path, vendored=False),
            extra_env={"UV_STUB_FAIL_STAGE": "2", "UV_STUB_FAIL_RC": str(_TIMEOUT_RC)},
        )

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert len(stage_runs(tmp_path)) == 3
        assert "NOT verified here" in completed.stderr
