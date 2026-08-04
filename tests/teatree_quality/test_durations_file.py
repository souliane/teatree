"""The one reader of ``dev/.test_durations`` (#4048)."""

import json
from pathlib import Path

import pytest

from teatree.quality.durations_file import DurationsUnreadableError, read_durations


class TestReadDurations:
    def test_an_absent_file_is_nothing_recorded(self, tmp_path: Path) -> None:
        assert read_durations(tmp_path / ".test_durations") == {}

    def test_recorded_seconds_are_floats(self, tmp_path: Path) -> None:
        path = tmp_path / ".test_durations"
        path.write_text(json.dumps({"tests/test_a.py::test_slow": 1}), encoding="utf-8")
        assert read_durations(path) == {"tests/test_a.py::test_slow": 1.0}

    def test_unparsable_json_is_unreadable_not_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".test_durations"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(DurationsUnreadableError):
            read_durations(path)

    def test_a_non_mapping_is_unreadable_not_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".test_durations"
        path.write_text(json.dumps([1, 2]), encoding="utf-8")
        with pytest.raises(DurationsUnreadableError):
            read_durations(path)

    def test_a_non_numeric_duration_is_unreadable_not_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".test_durations"
        path.write_text(json.dumps({"tests/test_a.py::test_slow": "quick"}), encoding="utf-8")
        with pytest.raises(DurationsUnreadableError):
            read_durations(path)

    def test_undecodable_bytes_are_unreadable_not_a_crash(self, tmp_path: Path) -> None:
        """``read_text`` raises ``UnicodeDecodeError`` — a ``ValueError``, neither ``OSError`` nor a JSON error.

        Escaping unwrapped takes down the whole ``t3 doctor check`` run: no caller
        guards this call, both checks catch only ``DurationsUnreadableError``, and
        the doctor has no global except.
        """
        path = tmp_path / ".test_durations"
        path.write_bytes(b'\xff\xfe{"a": 1}')
        with pytest.raises(DurationsUnreadableError):
            read_durations(path)
