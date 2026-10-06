# test-path: cross-cutting
"""``loop-registry.json``'s cold WRITER and Django READER resolve one directory (#3828).

The Django-free hook tier owns the file — :mod:`hooks.scripts.hook_router` resolves its
path and writes the attended loop-slot owner — while the Django tier only reads it, through
:func:`teatree.utils.hook_registry.loop_registry_dir`. Two resolvers, one file: with an XDG
sandbox set the reader once looked in ``~/.local/share/teatree`` while the hook wrote under
``$XDG_DATA_HOME/teatree`` (the #3499 failure verbatim).
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.core.session_identity import current_session_pid
from teatree.utils.hook_registry import loop_registry_dir

_OWNER_PID = 4242


def _write_owner() -> None:
    router._write_loop_registry({router._OWNER_LOOP: {"session_id": "s", "pid": _OWNER_PID}})


class TestLoopRegistryDirectoryParity:
    @pytest.fixture(autouse=True)
    def _no_pid_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_LOOP_SESSION_PID", raising=False)

    def test_xdg_data_home_moves_both_tiers_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_LOOP_REGISTRY_DIR", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        _write_owner()

        assert (tmp_path / "xdg" / "teatree" / "loop-registry.json").is_file()
        assert current_session_pid() == _OWNER_PID

    def test_the_override_wins_over_xdg_in_both_tiers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        override = tmp_path / "override"
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(override))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        _write_owner()

        assert (override / "loop-registry.json").is_file()
        assert not (tmp_path / "xdg" / "teatree" / "loop-registry.json").exists()
        assert current_session_pid() == _OWNER_PID

    @pytest.mark.parametrize(
        ("env", "expected_subpath"),
        [
            ({"T3_LOOP_REGISTRY_DIR": "override"}, "override"),
            ({"XDG_DATA_HOME": "xdg"}, "xdg/teatree"),
            ({}, "home/.local/share/teatree"),
        ],
    )
    def test_the_django_resolver_mirrors_the_hooks_precedence(
        self, env: dict[str, str], expected_subpath: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("T3_LOOP_REGISTRY_DIR", "XDG_DATA_HOME"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        for name, value in env.items():
            monkeypatch.setenv(name, str(tmp_path / value))

        assert loop_registry_dir() == tmp_path / expected_subpath
        assert loop_registry_dir() == router._loop_registry_path().parent
