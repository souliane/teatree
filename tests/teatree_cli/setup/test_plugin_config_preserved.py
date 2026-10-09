"""Plugin registration never rewrites a Claude config it could not read.

Every write path here is a read-modify-write of an operator-owned file. Parsing
an unreadable ``settings.json`` down to ``{}`` and writing that back replaced the
operator's hooks, permissions and env with the two keys this module manages —
a silent, total loss of their configuration on a routine ``t3 setup``.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.setup.plugin_registrar import PluginRegistrar, PyrightPluginRegistrar

_MALFORMED = '{"enabledPlugins": {"t3@souliane": true},'


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda _cls: tmp_path))
    (tmp_path / ".claude" / "plugins").mkdir(parents=True)
    return tmp_path


class TestMalformedSettingsAreLeftIntact:
    def test_install_refuses_rather_than_overwriting_settings(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text(_MALFORMED, encoding="utf-8")
        repo = fake_home / "teatree-clone"
        repo.mkdir()

        assert PluginRegistrar(repo).install() is False

        assert settings.read_text(encoding="utf-8") == _MALFORMED
        assert "refusing to rewrite" in capsys.readouterr().out

    def test_install_refuses_rather_than_overwriting_installed_plugins(self, fake_home: Path) -> None:
        installed = fake_home / ".claude" / "plugins" / "installed_plugins.json"
        installed.write_text(_MALFORMED, encoding="utf-8")
        repo = fake_home / "teatree-clone"
        repo.mkdir()

        assert PluginRegistrar(repo).install() is False

        assert installed.read_text(encoding="utf-8") == _MALFORMED

    def test_a_top_level_json_list_is_not_flattened_to_an_object(self, fake_home: Path) -> None:
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text("[]", encoding="utf-8")
        repo = fake_home / "teatree-clone"
        repo.mkdir()

        assert PluginRegistrar(repo).install() is False

        assert settings.read_text(encoding="utf-8") == "[]"

    def test_pyright_registration_reports_and_skips(self, fake_home: Path) -> None:
        installed = fake_home / ".claude" / "plugins" / "installed_plugins.json"
        installed.write_text(_MALFORMED, encoding="utf-8")

        assert PyrightPluginRegistrar().install() is False

        assert installed.read_text(encoding="utf-8") == _MALFORMED

    def test_langserver_provisioning_reports_it_cannot_tell(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (fake_home / ".claude" / "settings.json").write_text(_MALFORMED, encoding="utf-8")

        assert PyrightPluginRegistrar.ensure_langserver() is False
        assert "Cannot tell whether pyright-lsp is enabled" in capsys.readouterr().out


class TestAReadableConfigStillRegisters:
    """ANTI-VACUITY: the refusal is keyed on unreadability, not on registration being broken."""

    def test_an_operator_owned_settings_file_keeps_its_other_keys(self, fake_home: Path) -> None:
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"env": {"KEEP": "me"}}), encoding="utf-8")
        repo = fake_home / "teatree-clone"
        repo.mkdir()

        assert PluginRegistrar(repo).install() is True

        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["env"] == {"KEEP": "me"}
        assert data["enabledPlugins"]["t3@souliane"] is True
