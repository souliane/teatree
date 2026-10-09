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


def _module(repo: Path, name: str) -> None:
    (repo / "src" / "teatree" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")


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

    def test_flags_undeclared_single_file_module(self, fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The #740 blind spot: a single-file module under src/teatree/ with no
        # [[modules]] entry has unconstrained cross-layer imports while
        # `tach check` stays green — the guard must flag it like a package.
        _module(fake_repo, "visual_qa")
        _tach(fake_repo, "teatree.core")
        assert guard.main() == 1
        assert "teatree.visual_qa" in capsys.readouterr().out

    def test_passes_when_single_file_module_declared(self, fake_repo: Path) -> None:
        _module(fake_repo, "visual_qa")
        _tach(fake_repo, "teatree.visual_qa")
        assert guard.main() == 0

    def test_dunder_single_file_modules_are_not_required(self, fake_repo: Path) -> None:
        # __init__.py / __main__.py are not declarable tach modules.
        _module(fake_repo, "__init__")
        _module(fake_repo, "__main__")
        _tach(fake_repo, "teatree.core")
        assert guard.main() == 0

    def test_allowlisted_leaf_module_is_not_required(self, fake_repo: Path) -> None:
        # Genuine leaf modules (no internal imports, no internal importers) may
        # be explicitly allowlisted instead of carrying an empty [[modules]].
        _module(fake_repo, "urls")
        guard_allow = guard.LEAF_MODULE_ALLOWLIST | {"teatree.urls"}
        import unittest.mock as m  # noqa: PLC0415

        with m.patch.object(guard, "LEAF_MODULE_ALLOWLIST", guard_allow):
            _tach(fake_repo, "teatree.core")
            assert guard.main() == 0

    def test_nested_core_module_declaration_does_not_disturb_top_level_check(self, fake_repo: Path) -> None:
        # #2385 declares nested teatree.core.<child> [[modules]] entries
        # (modelkit/managers/models). The hook only checks top-level units, so a
        # nested entry neither satisfies a missing top-level package nor is
        # flagged as undeclared — the top-level teatree.core declaration alone
        # keeps the gate green.
        _package(fake_repo, "core")
        _tach(fake_repo, "teatree.core", "teatree.core.modelkit")
        assert guard.main() == 0

    def test_nested_entry_alone_does_not_satisfy_missing_top_level_core(self, fake_repo: Path) -> None:
        # A nested teatree.core.modelkit entry without the top-level teatree.core
        # entry must still flag teatree.core as undeclared.
        _package(fake_repo, "core")
        _tach(fake_repo, "teatree.core.modelkit")
        assert guard.main() == 1
