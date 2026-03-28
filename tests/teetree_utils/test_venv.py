import sys
from pathlib import Path

import pytest

from teetree.utils.venv import find_activate, find_python


@pytest.fixture
def venv_tree(tmp_path: Path) -> Path:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").touch()
    (tmp_path / ".venv" / "bin" / "activate").touch()
    return tmp_path


class TestFindPython:
    def test_uses_virtual_env_when_set(self, venv_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", str(venv_tree / ".venv"))
        assert find_python() == str(venv_tree / ".venv" / "bin" / "python")

    def test_falls_back_to_local_venv(self, venv_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        assert find_python(venv_tree) == str(venv_tree / ".venv" / "bin" / "python")

    def test_falls_back_to_sys_executable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        assert find_python(tmp_path) == sys.executable


class TestFindActivate:
    def test_uses_virtual_env_when_set(self, venv_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", str(venv_tree / ".venv"))
        assert find_activate() == str(venv_tree / ".venv" / "bin" / "activate")

    def test_falls_back_to_local_venv(self, venv_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        assert find_activate(venv_tree) == str(venv_tree / ".venv" / "bin" / "activate")

    def test_returns_empty_when_no_venv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        assert find_activate(tmp_path) == ""
