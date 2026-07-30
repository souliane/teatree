from pathlib import Path

import pytest

from teatree.agents.lane_b.filesystem import PathTraversalError, build_filesystem_toolset, resolve_within


class TestResolveWithin:
    def test_relative_path_joins_onto_root(self, tmp_path: Path) -> None:
        assert resolve_within(tmp_path, "sub/file.txt") == (tmp_path / "sub/file.txt").resolve()

    def test_dotdot_traversal_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PathTraversalError):
            resolve_within(tmp_path, "../escape.txt")

    def test_absolute_path_outside_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PathTraversalError):
            resolve_within(tmp_path, "/etc/passwd")

    def test_absolute_path_inside_root_is_allowed(self, tmp_path: Path) -> None:
        inside = tmp_path / "ok.txt"
        assert resolve_within(tmp_path, str(inside)) == inside.resolve()

    def test_symlink_escape_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside_dir"
        outside.mkdir()
        (tmp_path / "link").symlink_to(outside)
        with pytest.raises(PathTraversalError):
            resolve_within(tmp_path, "link/secret.txt")


def _tool(toolset, name):
    return toolset.tools[name].function


class TestFilesystemTools:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        ts = build_filesystem_toolset(tmp_path)
        _tool(ts, "Write")("notes/a.txt", "hello")
        assert (tmp_path / "notes/a.txt").read_text() == "hello"
        assert _tool(ts, "Read")("notes/a.txt") == "hello"

    def test_edit_replaces_first_occurrence(self, tmp_path: Path) -> None:
        ts = build_filesystem_toolset(tmp_path)
        (tmp_path / "f.txt").write_text("aXaXa")
        _tool(ts, "Edit")("f.txt", "X", "Y")
        assert (tmp_path / "f.txt").read_text() == "aYaXa"

    def test_edit_missing_substring_raises(self, tmp_path: Path) -> None:
        ts = build_filesystem_toolset(tmp_path)
        (tmp_path / "f.txt").write_text("abc")
        with pytest.raises(ValueError, match="substring not found"):
            _tool(ts, "Edit")("f.txt", "zzz", "y")

    def test_search_finds_matching_files(self, tmp_path: Path) -> None:
        ts = build_filesystem_toolset(tmp_path)
        (tmp_path / "a.py").write_text("needle here")
        (tmp_path / "b.py").write_text("nothing")
        assert _tool(ts, "Grep")("needle") == ["a.py"]

    def test_read_only_toolset_has_no_write_tools(self, tmp_path: Path) -> None:
        ts = build_filesystem_toolset(tmp_path, allow_write=False)
        assert "Read" in ts.tools
        assert "Grep" in ts.tools
        assert "Write" not in ts.tools
        assert "Edit" not in ts.tools

    def test_write_outside_root_is_refused(self, tmp_path: Path) -> None:
        ts = build_filesystem_toolset(tmp_path)
        with pytest.raises(PathTraversalError):
            _tool(ts, "Write")("../escape.txt", "x")
