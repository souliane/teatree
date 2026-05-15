"""Tests for the validate-mr-metadata PreToolUse hook (#119 Part 3).

The hook was a permanent no-op because it was gated behind
``T3_MR_VALIDATE_SCRIPT``, which is never set anywhere. The fix makes it
invoke ``t3 tool validate-mr`` (the active overlay's ``validate_pr``) BY
DEFAULT so a bad MR title/description is rejected BEFORE the push, every
time, with no opt-in. The env var remains an optional override.
"""

import json
import subprocess
from unittest.mock import patch

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_validate_mr_metadata


def _glab_create(title: str, description: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {
            "command": f"glab mr create --title '{title}' --description '{description}'",
        },
    }


class TestDefaultOverlayValidation:
    """No T3_MR_VALIDATE_SCRIPT set -> validate via `t3 tool validate-mr`."""

    def test_blocks_when_overlay_validator_rejects(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")

        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Title is empty.\nMR description is empty.\n"
        )
        with patch.object(router.subprocess, "run", return_value=completed) as run:
            blocked = handle_validate_mr_metadata(_glab_create("", ""))

        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "Title is empty." in out["permissionDecisionReason"]
        # Invoked the default `t3 tool validate-mr` path.
        argv = run.call_args[0][0]
        assert argv[:3] == ["/usr/local/bin/t3", "tool", "validate-mr"]
        assert "--title" in argv
        assert "--description" in argv

    def test_allows_when_overlay_validator_passes(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is False

    def test_noop_when_not_a_glab_mr_command(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        data = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        assert handle_validate_mr_metadata(data) is False

    def test_noop_when_t3_not_on_path(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        # No t3 binary -> cannot validate -> fail open (don't block the agent
        # on a broken environment), same posture as other t3-shelling hooks.
        assert handle_validate_mr_metadata(_glab_create("bad", "bad")) is False

    def test_missing_title_is_validated_not_skipped(self, monkeypatch, capsys):
        # An MR create with no --title is exactly the bad metadata the gate
        # must reject — it must be validated, not silently skipped (#119).
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "glab mr create --description 'x'"}}
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Title is empty.")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_validate_mr_metadata(data)
        assert blocked is True
        argv = run.call_args[0][0]
        assert argv[:3] == ["/usr/local/bin/t3", "tool", "validate-mr"]


class TestEnvVarOverrideStillWorks:
    """An explicitly-set T3_MR_VALIDATE_SCRIPT remains the override path."""

    def test_uses_script_when_env_var_set(self, monkeypatch, tmp_path):
        script = tmp_path / "v.py"
        script.write_text("import sys; sys.exit(0)", encoding="utf-8")
        monkeypatch.setenv("T3_MR_VALIDATE_SCRIPT", str(script))
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok) as run:
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "body"))
        assert blocked is False
        # Used the script, not `t3 tool validate-mr`.
        argv = run.call_args[0][0]
        assert str(script) in argv
        assert "validate-mr" not in argv
