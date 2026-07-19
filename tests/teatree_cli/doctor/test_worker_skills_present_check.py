"""``_check_worker_skills_present`` — the `t3 doctor` skill-less-worker HARD gate.

Owner principle: PREFER HARD FAIL over running with a critical capability missing.
A worker whose agents load ZERO skills is a broken product, so when doctor runs in
the ``worker`` role and the ``t3@souliane`` plugin is not registered/enabled, this
FAILs (gates the exit code + the watchdog owner DM) — it is NOT a soft WARN. The lean
admin/slack-listener (which do not run the loop's agents) and a roleless host
invocation are skipped. Mirrors the entrypoint's ``verify_agent_skills`` precondition.

``~/.claude`` is stubbed under ``tmp_path`` so every branch is deterministic.
"""

import json
from pathlib import Path

from teatree.cli.doctor.checks_resources import (
    _CLAUDE_PLUGIN_ID,
    _check_worker_skills_present,
    _worker_skills_registered,
)


def _claude_home(tmp_path: Path, *, enabled: bool, installed: bool, install_dir_exists: bool = True) -> Path:
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": {_CLAUDE_PLUGIN_ID: enabled}}), encoding="utf-8"
    )
    install_path = home / "clone"
    if install_dir_exists:
        install_path.mkdir()
    plugins = {"plugins": {_CLAUDE_PLUGIN_ID: [{"installPath": str(install_path)}]}} if installed else {"plugins": {}}
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(plugins), encoding="utf-8")
    return home


class TestWorkerSkillsRegistered:
    def test_true_when_enabled_and_installed_with_resolvable_path(self, tmp_path: Path) -> None:
        assert _worker_skills_registered(_claude_home(tmp_path, enabled=True, installed=True)) is True

    def test_false_when_not_enabled(self, tmp_path: Path) -> None:
        assert _worker_skills_registered(_claude_home(tmp_path, enabled=False, installed=True)) is False

    def test_false_when_not_installed(self, tmp_path: Path) -> None:
        assert _worker_skills_registered(_claude_home(tmp_path, enabled=True, installed=False)) is False

    def test_false_when_install_path_missing_on_disk(self, tmp_path: Path) -> None:
        home = _claude_home(tmp_path, enabled=True, installed=True, install_dir_exists=False)
        assert _worker_skills_registered(home) is False

    def test_false_when_claude_absent(self, tmp_path: Path) -> None:
        assert _worker_skills_registered(tmp_path / "empty") is False


class TestWorkerSkillsPresentCheck:
    def test_worker_without_skills_hard_fails(self, tmp_path: Path, capsys) -> None:
        home = _claude_home(tmp_path, enabled=False, installed=False)
        assert _check_worker_skills_present(role="worker", home=home) is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert _CLAUDE_PLUGIN_ID in out

    def test_worker_with_skills_is_ok(self, tmp_path: Path, capsys) -> None:
        home = _claude_home(tmp_path, enabled=True, installed=True)
        assert _check_worker_skills_present(role="worker", home=home) is True
        assert capsys.readouterr().out == ""

    def test_non_worker_role_is_skipped(self, tmp_path: Path, capsys) -> None:
        # Admin/slack-listener do not run the loop's agents — a missing plugin must NOT fail them.
        home = _claude_home(tmp_path, enabled=False, installed=False)
        assert _check_worker_skills_present(role="admin", home=home) is True
        assert capsys.readouterr().out == ""

    def test_roleless_host_is_skipped(self, tmp_path: Path, capsys) -> None:
        home = _claude_home(tmp_path, enabled=False, installed=False)
        assert _check_worker_skills_present(role="", home=home) is True
        assert capsys.readouterr().out == ""
