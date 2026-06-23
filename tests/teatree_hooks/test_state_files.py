"""Generic per-session state-file IO helpers (PR #2661 extraction).

``read_lines`` / ``append_line`` were factored out of ``hook_router`` into the
``state_files`` sibling so the over-cap router stays shrink-only. These tests
pin the small contract directly.
"""

import sys
from pathlib import Path

# The router puts its own dir on sys.path; mirror that so the bare sibling
# import resolves in the test process too.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from state_files import append_line, read_lines  # noqa: E402


class TestReadLines:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_lines(tmp_path / "nope.txt") == []

    def test_returns_non_empty_stripped_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("a\n\n  b  \n\nc\n", encoding="utf-8")
        assert read_lines(f) == ["a", "  b  ", "c"]

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("\n\n", encoding="utf-8")
        assert read_lines(f) == []


class TestAppendLine:
    def test_appends_with_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        append_line(f, "first")
        append_line(f, "second")
        assert f.read_text(encoding="utf-8") == "first\nsecond\n"

    def test_round_trips_through_read_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        append_line(f, "x")
        append_line(f, "y")
        assert read_lines(f) == ["x", "y"]
