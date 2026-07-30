"""Symlink-target health check — edge cases for worktree readiness."""

from pathlib import Path

from teatree.core.worktree.health import _symlink_source_healthy


class TestSymlinkSourceHealthy:
    def test_symlink_with_existing_source_file(self, tmp_path: Path) -> None:
        source = tmp_path / "src.txt"
        source.write_text("hi")
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is True

    def test_symlink_with_missing_source_is_unhealthy(self, tmp_path: Path) -> None:
        source = tmp_path / "absent"
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is False

    def test_symlink_to_empty_directory_is_unhealthy(self, tmp_path: Path) -> None:
        source = tmp_path / "empty-dir"
        source.mkdir()
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is False

    def test_symlink_to_populated_directory_is_healthy(self, tmp_path: Path) -> None:
        source = tmp_path / "full-dir"
        source.mkdir()
        (source / "child").write_text("x")
        dest = tmp_path / "link"
        dest.symlink_to(source)
        assert _symlink_source_healthy(dest, source) is True

    def test_real_file_dest_is_healthy(self, tmp_path: Path) -> None:
        dest = tmp_path / "real.txt"
        dest.write_text("ok")
        assert _symlink_source_healthy(dest, tmp_path / "ignored") is True

    def test_missing_dest_is_unhealthy(self, tmp_path: Path) -> None:
        assert _symlink_source_healthy(tmp_path / "absent", tmp_path / "also-absent") is False

    def test_real_directory_dest_empty_is_unhealthy(self, tmp_path: Path) -> None:
        dest = tmp_path / "real-dir"
        dest.mkdir()
        assert _symlink_source_healthy(dest, tmp_path / "ignored") is False

    def test_real_directory_dest_with_children_is_healthy(self, tmp_path: Path) -> None:
        dest = tmp_path / "real-dir"
        dest.mkdir()
        (dest / "child").write_text("x")
        assert _symlink_source_healthy(dest, tmp_path / "ignored") is True
