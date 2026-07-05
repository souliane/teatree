"""`t3 setup` installs the top-level statusLine block, never clobbering (PR-17)."""

import json
from pathlib import Path

from teatree.cli.setup.statusline_installer import StatuslineInstall, install_statusline, statusline_command_path


def _repo(tmp_path: Path) -> Path:
    script = tmp_path / "hooks" / "scripts" / "statusline.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return tmp_path


class TestStatuslineCommandPath:
    def test_absolute_path_to_hook_script(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        path = statusline_command_path(repo)
        assert Path(path).is_absolute()
        assert path.endswith("hooks/scripts/statusline.sh")


class TestInstallStatusline:
    def test_creates_block_when_settings_absent(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        settings = tmp_path / "cfg" / "settings.json"
        assert install_statusline(settings, repo) is StatuslineInstall.INSTALLED
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["statusLine"]["type"] == "command"
        assert Path(data["statusLine"]["command"]).is_absolute()

    def test_adds_block_preserving_existing_keys(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(t3:*)"]}}), encoding="utf-8")
        assert install_statusline(settings, repo) is StatuslineInstall.INSTALLED
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["permissions"]["allow"] == ["Bash(t3:*)"]
        assert "statusLine" in data

    def test_never_clobbers_existing_statusline(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "/my/own.sh"}}), encoding="utf-8")
        assert install_statusline(settings, repo) is StatuslineInstall.ALREADY_PRESENT
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["statusLine"]["command"] == "/my/own.sh"

    def test_unparseable_settings_left_alone(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text("{ not json", encoding="utf-8")
        assert install_statusline(settings, repo) is StatuslineInstall.UNREADABLE
        assert settings.read_text(encoding="utf-8") == "{ not json"
