"""Tests for ``PyrightPluginRegistrar`` — external pyright-lsp plugin registration.

``t3 setup`` registers + enables the ``pyright-lsp@claude-plugins-official`` plugin
so factory agents get LIVE pyright type diagnostics while coding. Unlike the local
``t3@souliane`` plugin (whose JSON is written directly), pyright-lsp lives in a
remote marketplace, so registration is driven through the ``claude plugin`` CLI. The
step is idempotent and offline-safe: an unreachable marketplace WARNs and continues
rather than aborting setup.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.setup.plugin_registrar import PyrightPluginRegistrar

_PLUGIN_ID = "pyright-lsp@claude-plugins-official"
_MARKETPLACE_SOURCE = "anthropics/claude-plugins-official"


def _read_enabled(home: Path) -> dict[str, object]:
    settings = home / ".claude" / "settings.json"
    if not settings.is_file():
        return {}
    return json.loads(settings.read_text()).get("enabledPlugins", {})


class TestPyrightPluginRegistrarInstall:
    def test_adds_marketplace_installs_and_enables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")

        calls: list[list[str]] = []

        class _Result:
            returncode = 0

        def _run(cmd: list[str], **_kwargs: object) -> _Result:
            calls.append(cmd)
            return _Result()

        monkeypatch.setattr("subprocess.run", _run)

        assert PyrightPluginRegistrar().install() is True

        # Both CLI steps ran, in order: marketplace add, then install.
        assert calls[0][1:] == ["plugin", "marketplace", "add", _MARKETPLACE_SOURCE]
        assert calls[1][1:] == ["plugin", "install", _PLUGIN_ID]
        # And the plugin is enabled in settings.json (the managed drift key).
        assert _read_enabled(tmp_path).get(_PLUGIN_ID) is True

    def test_skips_network_when_already_installed_but_still_enables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        # A live installed_plugins.json entry with an existing installPath dir.
        install_path = tmp_path / "cache" / "pyright-lsp"
        install_path.mkdir(parents=True)
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {_PLUGIN_ID: [{"installPath": str(install_path)}]}})
        )

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "the claude CLI must not be shelled out when already installed"
            raise AssertionError(msg)

        monkeypatch.setattr("subprocess.run", _boom)
        monkeypatch.setattr("shutil.which", _boom)

        assert PyrightPluginRegistrar().install() is True
        assert _read_enabled(tmp_path).get(_PLUGIN_ID) is True

    def test_offline_marketplace_add_warns_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")

        class _Result:
            returncode = 1  # marketplace add fails (offline)

        monkeypatch.setattr("subprocess.run", lambda _cmd, **_kwargs: _Result())

        assert PyrightPluginRegistrar().install() is False  # non-fatal
        out = capsys.readouterr().out
        assert "WARN" in out
        # Never enabled when it could not be registered.
        assert _read_enabled(tmp_path).get(_PLUGIN_ID) is None

    def test_missing_claude_cli_warns_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("shutil.which", lambda _name: None)

        assert PyrightPluginRegistrar().install() is False
        assert "WARN" in capsys.readouterr().out

    def test_run_claude_returns_false_on_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415 — local to the test that stubs it

        def _timeout(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        monkeypatch.setattr("subprocess.run", _timeout)
        assert PyrightPluginRegistrar._run_claude("/usr/bin/claude", "plugin", "list") is False
