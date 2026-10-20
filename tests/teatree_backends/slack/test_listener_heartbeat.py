import json
import time
from pathlib import Path

from teatree.backends.slack.listener_heartbeat import write_heartbeat
from teatree.core.slack_first.slack_listener_heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_is_stale,
    read_heartbeat,
)


def test_stamps_are_per_overlay(tmp_path: Path) -> None:
    path = tmp_path / "beat.json"

    first = write_heartbeat("a", path)
    second = write_heartbeat("b", path)

    beat = read_heartbeat(path)
    assert beat is not None
    assert beat.stamp_for("a") == first
    assert beat.stamp_for("b") == second
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["updated_at"] == int(second)
    assert raw["interval_seconds"] == HEARTBEAT_INTERVAL_SECONDS


def test_a_legacy_payload_applies_to_every_overlay(tmp_path: Path) -> None:
    path = tmp_path / "beat.json"
    path.write_text(json.dumps({"updated_at": 1000, "interval_seconds": 15}), encoding="utf-8")

    beat = read_heartbeat(path)

    assert beat is not None
    assert beat.stamp_for("any") == 1000
    assert heartbeat_is_stale(beat, "any", now=1000 + 60) is False
    assert heartbeat_is_stale(beat, "any", now=1000 + 121) is True


def test_stamping_one_overlay_preserves_the_legacy_stamp_for_other_overlays(tmp_path: Path) -> None:
    path = tmp_path / "beat.json"
    path.write_text(json.dumps({"updated_at": 1000, "interval_seconds": 15}), encoding="utf-8")

    write_heartbeat("a", path)

    beat = read_heartbeat(path)
    assert beat is not None
    assert beat.stamp_for("b") == 1000


def test_absent_file_or_missing_stamp_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "beat.json"
    assert read_heartbeat(path) is None
    assert heartbeat_is_stale(None, "a", now=time.time()) is True

    write_heartbeat("a", path)
    beat = read_heartbeat(path)

    assert heartbeat_is_stale(beat, "a", now=time.time()) is False
    assert heartbeat_is_stale(beat, "b", now=time.time()) is True


def test_an_unparsable_file_reads_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "beat.json"
    path.write_text("{not json", encoding="utf-8")

    assert read_heartbeat(path) is None
