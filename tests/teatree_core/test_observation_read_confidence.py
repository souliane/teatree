"""The dashboard's best-effort rows and the incident scanner's confidence differ."""

import datetime as dt
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.telemetry import admission as telemetry

NOW = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)


def test_admission_schema_is_a_separate_module_with_compatible_validator_reexports() -> None:
    assert importlib.util.find_spec("teatree.core.telemetry") is not None
    assert importlib.util.find_spec("teatree.core.telemetry.admission_schema") is not None
    from teatree.core.telemetry import admission_schema as schema  # noqa: PLC0415 — test the re-export boundary

    assert telemetry._valid_observation is schema._valid_observation
    assert telemetry._valid_factory_row is schema._valid_factory_row
    assert telemetry._valid_lifecycle_row is schema._valid_lifecycle_row
    assert telemetry._safe_admission_reason is schema._safe_admission_reason


def _row() -> dict:
    return {
        "epoch": int(NOW.timestamp()),
        "cause": "swap",
        "pressure": 0.91,
        "band": "shed",
        "admit": False,
        "reason": "unknown",
        "lane": "loop",
    }


def _path(directory: Path) -> Path:
    return directory / f"admission-{NOW.date().isoformat()}.jsonl"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"{bad\n", "otel_malformed"),
        (b"\xff\n", "otel_malformed"),
        (json.dumps(_row()).encode(), "otel_truncated"),
    ],
)
def test_corrupt_or_truncated_telemetry_never_proves_recovery(tmp_path: Path, payload: bytes, reason: str) -> None:
    _path(tmp_path).write_bytes(payload)

    result = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)

    assert result.complete is False
    assert result.reason == reason


def test_missing_and_unreadable_telemetry_are_distinct_unknowns(tmp_path: Path) -> None:
    missing = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)
    assert missing.rows == []
    assert missing.complete is False
    assert missing.reason == "otel_missing"

    _path(tmp_path).mkdir()
    unreadable = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)
    assert unreadable.complete is False
    assert unreadable.reason == "otel_unreadable"


def test_bounded_tail_keeps_positive_rows_but_cannot_prove_absence(tmp_path: Path) -> None:
    row = json.dumps(_row()).encode() + b"\n"
    _path(tmp_path).write_bytes(row * 3)
    with patch.object(telemetry, "READ_LIMIT_BYTES", len(row) * 4):
        result = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)
        dashboard_rows = telemetry.recent_pressure_observations(directory=tmp_path, now=NOW)

    assert result.complete is False
    assert result.reason == "otel_bounded"
    assert result.rows == dashboard_rows
    assert result.rows


def test_daily_pressure_budget_covers_worst_reason_at_five_second_cadence() -> None:
    row = _row() | {
        "reason": (
            "13 GB available at/under the 14 GB watermark "
            "(impossible admission ceiling: the cgroup memory cap is 16 GiB but a braked governor "
            "only re-admits above RAM_RESUME_FLOOR_GB=18 GiB, so once braked this lane can NEVER resume. "
            "Raise the container mem_limit above the resume floor (or lower the floor).)"
        )
    }
    assert telemetry._valid_observation(row, cutoff=1, current=int(NOW.timestamp()))
    row_bytes = len((json.dumps(row, separators=(",", ":")) + "\n").encode())
    daily_five_second_rows = 24 * 60 * 60 // 5

    assert row_bytes * daily_five_second_rows * 2 < telemetry.READ_LIMIT_BYTES // 2


def test_existing_empty_file_is_a_complete_clean_scan(tmp_path: Path) -> None:
    _path(tmp_path).write_bytes(b"")

    result = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)

    assert result.complete is True
    assert result.rows == []
    assert result.reason is None


def test_deeply_nested_json_is_unknown_without_aborting_scan(tmp_path: Path) -> None:
    _path(tmp_path).write_bytes(b"[" * 30_000 + b"]" * 30_000 + b"\n")

    result = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)

    assert result.complete is False
    assert result.reason == "otel_malformed"


def test_wrong_type_pressure_band_is_unknown_without_aborting_scan(tmp_path: Path) -> None:
    _path(tmp_path).write_text(json.dumps(_row() | {"band": []}) + "\n")

    result = telemetry.checked_pressure_observations(directory=tmp_path, now=NOW)

    assert result.complete is False
    assert result.reason == "otel_malformed"
