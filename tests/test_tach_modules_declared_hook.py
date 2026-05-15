from pathlib import Path

import pytest

import scripts.hooks.check_tach_modules_declared as guard


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pkg_root = tmp_path / "src" / "teatree"
    pkg_root.mkdir(parents=True)
    monkeypatch.setattr(guard, "PACKAGE_ROOT", pkg_root)
    monkeypatch.setattr(guard, "TACH_TOML", tmp_path / "tach.toml")
    return tmp_path


def _package(repo: Path, name: str) -> None:
    pkg = repo / "src" / "teatree" / name
    pkg.mkdir()
    (pkg / "__init__.py").touch()


def _tach(repo: Path, *paths: str) -> None:
    body = "\n".join(f'[[modules]]\npath = "{p}"\ndepends_on = []\n' for p in paths)
    (repo / "tach.toml").write_text(body, encoding="utf-8")


class TestTachModulesDeclaredHook:
    def test_passes_when_every_package_declared(self, fake_repo: Path) -> None:
        _package(fake_repo, "loop")
        _package(fake_repo, "docker")
        _tach(fake_repo, "teatree.loop", "teatree.docker")
        assert guard.main() == 0

    def test_flags_undeclared_package(self, fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _package(fake_repo, "loop")
        _tach(fake_repo, "teatree.core")
        assert guard.main() == 1
        assert "teatree.loop" in capsys.readouterr().out

    def test_ignores_non_package_directories(self, fake_repo: Path) -> None:
        (fake_repo / "src" / "teatree" / "__pycache__").mkdir()
        _tach(fake_repo, "teatree.core")
        assert guard.main() == 0

    def test_submodule_entry_does_not_satisfy_top_level_package(self, fake_repo: Path) -> None:
        _package(fake_repo, "core")
        _tach(fake_repo, "teatree.core.management")
        assert guard.main() == 1
