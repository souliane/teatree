"""The interactive session's permission posture is ADVISED, never enforced.

``permissions.defaultMode`` lives in the operator's own Claude Code settings, so
teatree can suggest a narrower posture but has no way to apply one. ``auto`` routes
each tool call past a model classifier — unprompted flow without blanket approval —
whereas ``bypassPermissions`` approves everything, which is correct for a HEADLESS
dispatch (no human present to approve a write) and needlessly wide for a session the
operator is sitting in front of.

Every branch returns ``True``: this check gates nothing.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.doctor.checks_session import _check_interactive_permission_mode


def _write_settings(home: Path, mode: str | None) -> None:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    permissions: dict[str, object] = {"allow": []}
    if mode is not None:
        permissions["defaultMode"] = mode
    (claude / "settings.json").write_text(json.dumps({"permissions": permissions}), encoding="utf-8")


class TestInteractivePermissionModeCheck:
    def test_auto_reports_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_settings(tmp_path, "auto")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _check_interactive_permission_mode() is True
        assert "auto" in capsys.readouterr().out

    def test_bypass_permissions_warns_and_names_the_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_settings(tmp_path, "bypassPermissions")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _check_interactive_permission_mode() is True, "advisory only — it must never gate"
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "defaultMode" in out, "the operator must be told which key to change"
        # The two lanes SHARE this file; only the per-dispatch --permission-mode pin
        # keeps them apart. The advice must say so, or an operator reasonably fears
        # that narrowing the interactive session will also throttle the factory.
        lowered = out.lower()
        assert "headless" in lowered, "must say the headless lane is unaffected"
        assert "--permission-mode" in lowered, "must name the pin that makes that true"

    def test_an_unset_mode_says_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No configured mode means the Claude Code default applies — not teatree's
        # business to nag about, so the check stays silent rather than guessing.
        _write_settings(tmp_path, None)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _check_interactive_permission_mode() is True
        assert capsys.readouterr().out == ""

    def test_absent_settings_file_says_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _check_interactive_permission_mode() is True
        assert capsys.readouterr().out == ""

    def test_unreadable_settings_degrades_silently_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Shares `_read_json_object` with the other checks that read this file, so a
        # corrupt settings.json reads as "no mode configured" and this check stays
        # quiet. Deliberate: two sibling checks already read the same file and degrade
        # the same way, and a third voice reporting one corrupt file is noise, not
        # signal. What must never happen is a crash — the doctor run has to finish.
        claude = tmp_path / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _check_interactive_permission_mode() is True
        assert capsys.readouterr().out == ""
