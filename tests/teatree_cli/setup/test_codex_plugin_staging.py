from pathlib import Path

import pytest

from teatree.cli.setup.codex_plugin_staging import codex_home, reclaiming_new_staging, staging_dirs


def _staging(home: Path, name: str) -> Path:
    path = home / "plugins" / "cache" / "souliane" / name
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(b"x" * 64)
    return path


def test_codex_home_prefers_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
    assert codex_home() == tmp_path / "custom"

    monkeypatch.delenv("CODEX_HOME")
    assert codex_home() == Path.home() / ".codex"


def test_staging_dirs_are_real_directories_only(tmp_path: Path) -> None:
    real = _staging(tmp_path, "plugin-install-a")
    (tmp_path / "plugins" / "cache" / "souliane" / "plugin-install-link").symlink_to(real)
    (tmp_path / "plugins" / "cache" / "souliane" / "t3").mkdir()

    assert staging_dirs(tmp_path) == {real}


def test_staging_created_during_the_call_is_removed_even_when_the_call_raises(tmp_path: Path) -> None:
    kept = _staging(tmp_path, "plugin-install-before")

    def _timed_out_add() -> None:
        with reclaiming_new_staging(tmp_path):
            _staging(tmp_path, "plugin-install-during")
            raise TimeoutError

    with pytest.raises(TimeoutError):
        _timed_out_add()

    assert staging_dirs(tmp_path) == {kept}


def test_a_missing_codex_home_is_not_an_error(tmp_path: Path) -> None:
    with reclaiming_new_staging(tmp_path / "absent"):
        pass

    assert staging_dirs(tmp_path / "absent") == set()
