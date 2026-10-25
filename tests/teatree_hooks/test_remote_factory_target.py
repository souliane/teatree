"""The cold SessionStart target probe reads operator state at call time."""

# test-path: cross-cutting — this probes a standalone hook module.

import ast
from pathlib import Path

import pytest

from hooks.scripts.remote_factory_target import remote_factory_target_active


@pytest.fixture
def target_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def test_no_target_is_local(target_dir: Path) -> None:
    assert remote_factory_target_active() is False


def test_named_file_is_remote(target_dir: Path) -> None:
    (target_dir / "factory-target").write_text("factory.example\n")
    assert remote_factory_target_active() is True


@pytest.mark.parametrize("content", ["", " \n\t", "# comment\n", "  # comment\n\n"])
def test_empty_or_comment_only_file_is_local(target_dir: Path, content: str) -> None:
    (target_dir / "factory-target").write_text(content)
    assert remote_factory_target_active() is False


@pytest.mark.parametrize("prefix", ["# comment\n", "\n  \n"])
def test_name_after_ignored_lines_is_remote(target_dir: Path, prefix: str) -> None:
    (target_dir / "factory-target").write_text(prefix + "factory.example\n")
    assert remote_factory_target_active() is True


def test_nonlocal_environment_target_is_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_FACTORY_TARGET", "factory.example")
    assert remote_factory_target_active() is True


@pytest.mark.parametrize("value", ["local", " local "])
def test_local_environment_overrides_file(target_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    (target_dir / "factory-target").write_text("factory.example\n")
    monkeypatch.setenv("T3_FACTORY_TARGET", value)
    assert remote_factory_target_active() is False


def test_blank_environment_uses_file(target_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (target_dir / "factory-target").write_text("factory.example\n")
    monkeypatch.setenv("T3_FACTORY_TARGET", " \t")
    assert remote_factory_target_active() is True


def test_xdg_data_directory_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_DATA_DIR", raising=False)
    xdg = tmp_path / "xdg"
    (xdg / "teatree").mkdir(parents=True)
    (xdg / "teatree" / "factory-target").write_text("factory.example\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert remote_factory_target_active() is True


def test_explicit_data_directory_wins_over_xdg(
    target_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "teatree").mkdir(parents=True)
    (xdg / "teatree" / "factory-target").write_text("factory.example\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert remote_factory_target_active() is False


def test_home_data_directory_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".local/share/teatree"
    root.mkdir(parents=True)
    (root / "factory-target").write_text("factory.example\n")
    assert remote_factory_target_active() is True


def test_non_directory_parent_is_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("no directory")
    monkeypatch.setenv("T3_DATA_DIR", str(root))
    assert remote_factory_target_active() is False


def test_unreadable_target_is_treated_as_remote(target_dir: Path) -> None:
    (target_dir / "factory-target").mkdir()
    assert remote_factory_target_active() is True


def test_invalid_utf8_target_is_treated_as_remote(target_dir: Path) -> None:
    (target_dir / "factory-target").write_bytes(b"\xff")
    assert remote_factory_target_active() is True


def test_cold_hook_imports_only_stdlib() -> None:
    source = Path(__file__).resolve().parents[2] / "hooks/scripts/remote_factory_target.py"
    tree = ast.parse(source.read_text())
    names = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    names.extend(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert set(names) <= {"os", "pathlib", "sys"}
