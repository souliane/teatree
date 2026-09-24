from pathlib import Path

import pytest

from teatree.agents.lane_b.filesystem import (
    _MAX_READ_BYTES,
    PathTraversalError,
    build_filesystem_toolset,
    resolve_within,
)


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


class TestSearchStaysInsideTheJail:
    """``Grep`` reaches files by glob, so the jail has to hold on that path too."""

    def test_traversing_glob_is_refused(self, tmp_path: Path) -> None:
        root, outside = tmp_path / "wt", tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("needle")
        ts = build_filesystem_toolset(root)
        with pytest.raises(PathTraversalError):
            _tool(ts, "Grep")("needle", "../outside/*")

    def test_symlink_to_a_file_outside_the_root_is_not_searched(self, tmp_path: Path) -> None:
        root, outside = tmp_path / "wt", tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("needle")
        (root / "link.txt").symlink_to(outside / "secret.txt")
        assert _tool(build_filesystem_toolset(root), "Grep")("needle") == []

    def test_no_unbounded_whole_file_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cap applied AFTER a whole-file read bounds the slice, not the memory:
        # a multi-gigabyte worktree file is fully resident before it is truncated.
        def refuse(*_args: object, **_kwargs: object) -> bytes:
            msg = "unbounded whole-file read"
            raise AssertionError(msg)

        (tmp_path / "big.txt").write_text("needle")
        ts = build_filesystem_toolset(tmp_path)
        monkeypatch.setattr(Path, "read_bytes", refuse)
        monkeypatch.setattr(Path, "read_text", refuse)
        assert _tool(ts, "Read")("big.txt") == "needle"
        assert _tool(ts, "Grep")("needle") == ["big.txt"]

    def test_read_stops_at_the_byte_cap(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * (_MAX_READ_BYTES + 500))
        assert len(_tool(build_filesystem_toolset(tmp_path), "Read")("big.txt")) == _MAX_READ_BYTES


class TestReadReachesTheHarnessSkillRoots:
    """A demoted skill's pointer names a ``SKILL.md`` outside the worktree; ``Read`` must reach it."""

    @pytest.fixture
    def layout(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        worktree, skills, outside = tmp_path / "wt", tmp_path / "skills", tmp_path / "outside"
        for directory in (worktree, skills / "code" / "references", outside):
            directory.mkdir(parents=True)
        (skills / "code" / "SKILL.md").write_text("code body")
        (skills / "code" / "references" / "tdd.md").write_text("tdd body")
        (outside / "secret.txt").write_text("secret")
        return worktree, skills, outside

    def _read(self, worktree: Path, skills: Path, path: str) -> str:
        return _tool(build_filesystem_toolset(worktree, read_only_roots=(skills,)), "Read")(path)

    def test_skill_body_is_readable(self, layout: tuple[Path, Path, Path]) -> None:
        worktree, skills, _ = layout
        assert self._read(worktree, skills, str(skills / "code" / "SKILL.md")) == "code body"

    def test_a_skill_reference_file_is_readable(self, layout: tuple[Path, Path, Path]) -> None:
        worktree, skills, _ = layout
        assert self._read(worktree, skills, str(skills / "code" / "references" / "tdd.md")) == "tdd body"

    def test_a_symlinked_skill_dir_is_readable(self, layout: tuple[Path, Path, Path]) -> None:
        worktree, skills, outside = layout
        (outside / "linked").mkdir()
        (outside / "linked" / "SKILL.md").write_text("linked body")
        (skills / "linked").symlink_to(outside / "linked")
        assert self._read(worktree, skills, str(skills / "linked" / "SKILL.md")) == "linked body"

    def test_the_skill_roots_are_not_readable_without_the_grant(self, layout: tuple[Path, Path, Path]) -> None:
        worktree, skills, _ = layout
        with pytest.raises(PathTraversalError):
            _tool(build_filesystem_toolset(worktree), "Read")(str(skills / "code" / "SKILL.md"))

    @pytest.mark.parametrize(
        "relative",
        ["code/../../outside/secret.txt", "code/../../../etc/passwd", "stray.md"],
    )
    def test_a_path_leaving_every_skill_dir_is_refused(self, layout: tuple[Path, Path, Path], relative: str) -> None:
        worktree, skills, _ = layout
        (skills / "stray.md").write_text("not inside a skill dir")
        with pytest.raises(PathTraversalError):
            self._read(worktree, skills, f"{skills}/{relative}")

    def test_a_symlink_escaping_the_skill_dir_is_refused(self, layout: tuple[Path, Path, Path]) -> None:
        worktree, skills, outside = layout
        (skills / "code" / "leak.md").symlink_to(outside / "secret.txt")
        with pytest.raises(PathTraversalError):
            self._read(worktree, skills, str(skills / "code" / "leak.md"))

    @pytest.mark.parametrize(("tool", "args"), [("Write", ("x",)), ("Edit", ("code", "x"))])
    def test_the_skill_roots_stay_unwritable(self, layout: tuple[Path, Path, Path], tool: str, args: tuple) -> None:
        worktree, skills, _ = layout
        ts = build_filesystem_toolset(worktree, read_only_roots=(skills,))
        with pytest.raises(PathTraversalError):
            _tool(ts, tool)(str(skills / "code" / "SKILL.md"), *args)
        assert (skills / "code" / "SKILL.md").read_text() == "code body"

    def test_search_does_not_walk_the_skill_roots(self, layout: tuple[Path, Path, Path]) -> None:
        worktree, skills, _ = layout
        assert _tool(build_filesystem_toolset(worktree, read_only_roots=(skills,)), "Grep")("code body") == []
