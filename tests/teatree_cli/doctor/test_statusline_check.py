"""`t3 doctor` verifies the statusLine block: presence/absolute/executable (PR-17)."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from teatree.cli.doctor.statusline import check_statusline


def _run(settings: Path) -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = check_statusline(settings_path=settings)
    return ok, out.getvalue()


def _executable_script(tmp_path: Path) -> Path:
    script = tmp_path / "statusline.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _write_settings(tmp_path: Path, command: str) -> Path:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": command}}), encoding="utf-8")
    return settings


class TestCheckStatusline:
    def test_passes_for_absolute_executable_command(self, tmp_path: Path) -> None:
        script = _executable_script(tmp_path)
        ok, message = _run(_write_settings(tmp_path, str(script)))
        assert ok is True
        assert "FAIL" not in message

    def test_passes_for_absolute_executable_command_with_arguments(self, tmp_path: Path) -> None:
        # A command carrying flags must validate its executable (the first shell
        # token), not the whole command string as one path (#3313).
        script = _executable_script(tmp_path)
        ok, message = _run(_write_settings(tmp_path, f"{script} --loop --json"))
        assert ok is True, message
        assert "FAIL" not in message

    def test_passes_for_tilde_anchored_command(self, tmp_path: Path) -> None:
        # A `~`-anchored command is absolute after home-expansion — it must not
        # be flagged "not absolute" (#3313). The conftest pins HOME to a tmp dir.
        script = Path.home() / "statusline.sh"
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        script.chmod(0o755)
        ok, message = _run(_write_settings(tmp_path, "~/statusline.sh --loop"))
        assert ok is True, message
        assert "FAIL" not in message

    def test_unparseable_command_string_fails(self, tmp_path: Path) -> None:
        ok, message = _run(_write_settings(tmp_path, 'a "b'))  # unbalanced quote
        assert ok is False
        assert "FAIL" in message
        assert "valid shell command" in message

    def test_missing_settings_warns_with_remediation(self, tmp_path: Path) -> None:
        ok, message = _run(tmp_path / "absent.json")
        assert ok is True
        assert "WARN" in message
        assert "t3 setup" in message

    def test_no_statusline_block_warns(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
        ok, message = _run(settings)
        assert ok is True
        assert "WARN" in message
        assert "t3 setup" in message

    def test_relative_path_fails_with_remediation(self, tmp_path: Path) -> None:
        ok, message = _run(_write_settings(tmp_path, "hooks/scripts/statusline.sh"))
        assert ok is False
        assert "FAIL" in message
        assert "absolute path" in message

    def test_missing_target_fails(self, tmp_path: Path) -> None:
        ok, message = _run(_write_settings(tmp_path, str(tmp_path / "gone.sh")))
        assert ok is False
        assert "FAIL" in message
        assert "missing" in message

    def test_non_executable_target_fails(self, tmp_path: Path) -> None:
        script = tmp_path / "statusline.sh"
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        script.chmod(0o644)
        ok, message = _run(_write_settings(tmp_path, str(script)))
        assert ok is False
        assert "FAIL" in message
        assert "not executable" in message
        assert "chmod +x" in message

    def test_unparseable_settings_warns(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{ not json", encoding="utf-8")
        ok, message = _run(settings)
        assert ok is True
        assert "WARN" in message


class TestPluginSettingsHasNoStatusline:
    """PR-17 constraint: the plugin (root) settings.json must NOT carry a statusLine."""

    def test_root_settings_json_has_no_statusline(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        settings = repo_root / "settings.json"
        assert settings.is_file()
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert "statusLine" not in data
