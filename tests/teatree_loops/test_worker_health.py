"""The supervisor's shared-volume availability heartbeat is fenced to init."""

import os
from pathlib import Path
from unittest.mock import patch

from teatree.loops import worker_health


def test_heartbeat_is_atomic_private_and_generation_matched(tmp_path: Path) -> None:
    (tmp_path / "boot-latest").write_text(
        "status=ready\nstage=ready\ncause=ready\nmissing=\ngeneration=" + "a" * 32 + "\n", encoding="ascii"
    )
    with (
        patch.dict(os.environ, {"T3_WORKER_HEALTH_GENERATION": "a" * 32}),
        patch.object(worker_health.time, "time", return_value=1234567890),
    ):
        worker_health.publish_worker_heartbeat("admits", active=True, data_dir=tmp_path)

    heartbeat = tmp_path / worker_health.HEARTBEAT_NAME
    assert worker_health.read_fields(heartbeat) == {
        "generation": "a" * 32,
        "epoch": "1234567890",
        "admission": "admits",
        "active": "1",
    }
    assert heartbeat.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".worker-health-*")) == []


def test_missing_process_generation_does_not_refresh_an_old_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / worker_health.HEARTBEAT_NAME
    heartbeat.write_text("stale", encoding="ascii")
    (tmp_path / "boot-latest").write_text("status=pending\ngeneration=" + "b" * 32 + "\n", encoding="ascii")
    with patch.dict(os.environ, {"T3_WORKER_HEALTH_GENERATION": ""}):
        worker_health.publish_worker_heartbeat("admits", active=True, data_dir=tmp_path)
    assert heartbeat.read_text(encoding="ascii") == "stale"


def test_old_worker_cannot_adopt_a_new_inits_generation(tmp_path: Path) -> None:
    marker = tmp_path / "boot-latest"
    marker.write_text(
        "status=ready\nstage=ready\ncause=ready\nmissing=\ngeneration=" + "a" * 32 + "\n", encoding="ascii"
    )
    with patch.dict(os.environ, {"T3_WORKER_HEALTH_GENERATION": "a" * 32}):
        worker_health.publish_worker_heartbeat("admits", active=True, data_dir=tmp_path)
        marker.write_text(
            "status=ready\nstage=ready\ncause=ready\nmissing=\ngeneration=" + "b" * 32 + "\n", encoding="ascii"
        )
        worker_health.publish_worker_heartbeat("admits", active=True, data_dir=tmp_path)
    fields = worker_health.read_fields(tmp_path / worker_health.HEARTBEAT_NAME)
    assert fields is not None
    assert fields["generation"] == "a" * 32


def test_deployed_heartbeat_uses_only_the_dedicated_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "health"
    state.mkdir()
    with patch.dict(
        os.environ,
        {"T3_WORKER_HEALTH_GENERATION": "a" * 32, "T3_WORKER_HEALTH_DIR": str(state)},
    ):
        worker_health.publish_worker_heartbeat("admits", active=True)
    fields = worker_health.read_fields(state / worker_health.HEARTBEAT_NAME)
    assert fields is not None
    assert fields["generation"] == "a" * 32
