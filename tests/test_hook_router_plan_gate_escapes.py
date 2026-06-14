# test-path: cross-cutting
# Exercises the hooks/scripts/hook_router.py gate (no src/teatree mirror) together
# with teatree.cli.teatree_gate and teatree.core.models, so it spans packages.
"""Anti-vacuous tests for the plan-gate never-lockout escapes (Batch B).

Three escape paths for ``handle_block_edit_before_planned``:

1. Per-call ``[skip-plan-gate: <reason>]`` token → ALLOW that call.
2. ``[teatree] plan_edit_gate_enabled = false`` kill-switch → ALLOW all calls.
3. ``t3 <overlay> gate plan disable`` self-rescue CLI → flips the kill-switch.

RED-on-revert proof embedded as docstrings: removing the token-check splice
flips the token-allow case to DENY (test 2 fails red).

These tests drive the real handler + real DB rows — no monkeypatching of
``_ticket_state_for_cwd`` — so the full resolution chain runs.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import tomlkit
from django.test import TestCase
from typer.testing import CliRunner

import hooks.scripts.hook_router as router
from teatree.cli.teatree_gate import PLAN_GATE_KEY, _gate_key_is_enabled, register_gate_commands
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


def _git_repo(path: Path) -> str:
    """Init a real git repo at *path* and return its resolved toplevel."""
    make_git_repo(path, initial_commit=False)
    return run_git(path, "rev-parse", "--show-toplevel")


def _capture_block(data: dict) -> tuple[bool, dict | None]:
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = router.handle_block_edit_before_planned(data)
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _started_ticket_in_repo(toplevel: str, repo_path: str = "backend") -> None:
    """Seed a STARTED ticket + Worktree row for *toplevel*."""
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
    Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path=repo_path,
        branch="99-x",
        extra={"worktree_path": toplevel},
    )


class TestSkipPlanGateToken(TestCase):
    """Per-call ``[skip-plan-gate: <reason>]`` token escape."""

    def _edit_input(self, cwd: str, *, new_string: str = "b") -> dict:
        return {
            "tool_name": "Edit",
            "cwd": cwd,
            "tool_input": {
                "file_path": f"{cwd}/foo.py",
                "old_string": "a",
                "new_string": new_string,
            },
        }

    def _write_input(self, cwd: str, *, content: str = "x") -> dict:
        return {
            "tool_name": "Write",
            "cwd": cwd,
            "tool_input": {
                "file_path": f"{cwd}/foo.py",
                "content": content,
            },
        }

    def test_plain_edit_on_started_ticket_is_denied(self) -> None:
        """Baseline deny: no token, started ticket → DENY (gate is live)."""
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            blocked, payload = _capture_block(self._edit_input(toplevel))
        assert blocked is True
        assert payload is not None

    def test_edit_carrying_skip_plan_gate_token_is_allowed(self) -> None:
        """[skip-plan-gate: trivial typo] in new_string → ALLOW.

        RED-on-revert: remove the ``_skip_plan_gate_token`` check from
        ``handle_block_edit_before_planned`` and this test flips to blocked=True.
        """
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            data = self._edit_input(toplevel, new_string="[skip-plan-gate: trivial typo] b")
            blocked, _ = _capture_block(data)
        assert blocked is False

    def test_write_carrying_skip_plan_gate_token_in_content_is_allowed(self) -> None:
        """[skip-plan-gate: add comment] in Write content → ALLOW."""
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            data = self._write_input(toplevel, content="[skip-plan-gate: add comment] x = 1")
            blocked, _ = _capture_block(data)
        assert blocked is False

    def test_empty_reason_in_skip_plan_gate_token_is_not_an_escape(self) -> None:
        """[skip-plan-gate:] with empty reason must not escape the gate."""
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            data = self._edit_input(toplevel, new_string="[skip-plan-gate:] b")
            blocked, _ = _capture_block(data)
        assert blocked is True

    def test_token_truncation_beyond_512_chars_is_not_an_escape(self) -> None:
        """A token buried beyond char 512 must not escape the gate."""
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            buried = "x" * 513 + "[skip-plan-gate: hidden]"
            data = self._edit_input(toplevel, new_string=buried)
            blocked, _ = _capture_block(data)
        assert blocked is True


class TestPlanGateKillSwitch(TestCase):
    """``[teatree] plan_edit_gate_enabled = false`` kill-switch escape."""

    def _edit_input(self, cwd: str) -> dict:
        return {
            "tool_name": "Edit",
            "cwd": cwd,
            "tool_input": {
                "file_path": f"{cwd}/foo.py",
                "old_string": "a",
                "new_string": "b",
            },
        }

    def test_gate_enabled_by_default_when_no_config(self) -> None:
        """``_plan_edit_gate_enabled`` returns True when no config exists."""
        with patch.object(router, "_plan_edit_gate_enabled", wraps=router._plan_edit_gate_enabled):
            with tempfile.NamedTemporaryFile(suffix=".toml", delete=True) as f:
                config_path = Path(f.name)
            with patch("hooks.scripts.hook_router.Path.home", return_value=config_path.parent):
                # File was deleted — no config exists at that path
                assert router._plan_edit_gate_enabled() is True

    def test_gate_off_config_allows_started_ticket_edit(self) -> None:
        """``plan_edit_gate_enabled = false`` in config → ALLOW even on started ticket."""
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            data = self._edit_input(toplevel)

            with patch.object(router, "_plan_edit_gate_enabled", return_value=False):
                blocked, _ = _capture_block(data)
        assert blocked is False

    def test_gate_on_config_still_denies_started_ticket_edit(self) -> None:
        """Explicit ``plan_edit_gate_enabled = true`` still denies a started ticket."""
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            _started_ticket_in_repo(toplevel)
            data = self._edit_input(toplevel)

            with patch.object(router, "_plan_edit_gate_enabled", return_value=True):
                blocked, _ = _capture_block(data)
        assert blocked is True


class TestPlanGateCLI(TestCase):
    """``t3 <overlay> gate plan disable/enable/status`` self-rescue CLI."""

    def test_plan_gate_key_constant_exists_in_teatree_gate(self) -> None:
        """PLAN_GATE_KEY is exported from ``teatree.cli.teatree_gate``."""
        assert PLAN_GATE_KEY == "plan_edit_gate_enabled"

    def test_gate_key_is_enabled_reads_plan_gate_key(self) -> None:
        """``_gate_key_is_enabled(PLAN_GATE_KEY)`` reads the right toml key."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".teatree.toml"
            doc = tomlkit.document()
            teatree_tbl = tomlkit.table()
            teatree_tbl[PLAN_GATE_KEY] = False
            doc["teatree"] = teatree_tbl
            config.write_text(tomlkit.dumps(doc), encoding="utf-8")
            with patch("teatree.cli.teatree_gate._config_path", return_value=config):
                assert _gate_key_is_enabled(PLAN_GATE_KEY) is False

    def test_gate_plan_subgroup_is_registered(self) -> None:
        """``t3 <overlay> gate plan status`` is reachable."""
        import typer  # noqa: PLC0415

        overlay_app = typer.Typer()
        register_gate_commands(overlay_app)
        runner = CliRunner()
        result = runner.invoke(overlay_app, ["gate", "plan", "status"])
        assert result.exit_code == 0
        assert "gate" in result.output.lower()

    def test_gate_plan_disable_writes_config(self) -> None:
        """``gate plan disable`` writes ``plan_edit_gate_enabled = false``."""
        import typer  # noqa: PLC0415

        overlay_app = typer.Typer()
        register_gate_commands(overlay_app)
        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".teatree.toml"
            with patch("teatree.cli.teatree_gate._config_path", return_value=config):
                result = runner.invoke(overlay_app, ["gate", "plan", "disable"])
                assert result.exit_code == 0, result.output
                with config.open("rb") as f:
                    import tomllib  # noqa: PLC0415

                    data = tomllib.load(f)
                assert data["teatree"][PLAN_GATE_KEY] is False

    def test_gate_plan_enable_writes_config(self) -> None:
        """``gate plan enable`` writes ``plan_edit_gate_enabled = true``."""
        import typer  # noqa: PLC0415

        overlay_app = typer.Typer()
        register_gate_commands(overlay_app)
        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".teatree.toml"
            with patch("teatree.cli.teatree_gate._config_path", return_value=config):
                result = runner.invoke(overlay_app, ["gate", "plan", "enable"])
                assert result.exit_code == 0, result.output
                with config.open("rb") as f:
                    import tomllib  # noqa: PLC0415

                    data = tomllib.load(f)
                assert data["teatree"][PLAN_GATE_KEY] is True
