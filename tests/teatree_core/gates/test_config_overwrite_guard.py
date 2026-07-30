"""Read-before-overwrite gate for tracked user config / dotfiles (PR #2661).

Two real incidents: a blind ``Write`` over a tracked dotfile (a symlink into
a dotfiles repo), and a near-``git checkout`` restore of a config without
reading the live on-disk content first. The gate FIRES (red) on a blind
overwrite/restore of a tracked config and PASSES (green) once the live content
was read this session.

Two layers are proven here: the pure decision core
(:mod:`teatree.core.gates.config_overwrite_guard`) and the live PreToolUse
hook handler (``config_overwrite_guard.handle_block_config_overwrite``) wired
through the router's ``<session>.reads`` state + ``_fail_open_or_deny`` chain.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from teatree.core.gates import config_overwrite_guard as core


class TestIsUserConfigPath:
    def test_dotfile_at_home_is_config(self) -> None:
        assert core.is_user_config_path("/Users/x/.appconfig.toml") is True
        assert core.is_user_config_path("/Users/x/.zshrc") is True

    def test_known_config_basename_is_config(self) -> None:
        assert core.is_user_config_path("/Users/x/config.toml") is True
        assert core.is_user_config_path("/anywhere/credentials.toml") is True

    def test_file_under_config_dir_is_config(self) -> None:
        assert core.is_user_config_path("/Users/x/.config/app/config.ini") is True
        assert core.is_user_config_path("/Users/x/dotfiles/app.conf") is True

    def test_any_file_under_xdg_config_is_config_regardless_of_suffix(self) -> None:
        # An XDG .config dir holds editor/app config of every shape — .lua, .json,
        # an extensionless file, a binary. All of it is user config.
        assert core.is_user_config_path("/Users/x/.config/nvim/init.lua") is True
        assert core.is_user_config_path("/Users/x/.config/app/data.json") is True
        assert core.is_user_config_path("/Users/x/.config/app/state") is True

    def test_source_tree_toml_is_not_config(self) -> None:
        assert core.is_user_config_path("/repo/src/teatree/pyproject.toml") is False
        assert core.is_user_config_path("/repo/src/module.py") is False
        # A .json deep in a source tree (not under .config / dotfiles) is NOT config.
        assert core.is_user_config_path("/repo/src/teatree/data.json") is False

    def test_empty_path_is_not_config(self) -> None:
        assert core.is_user_config_path("") is False


class TestFindBlindWrite:
    def test_fires_on_blind_overwrite_of_existing_unread_config(self) -> None:
        finding = core.find_blind_write("/Users/x/.appconfig.toml", exists=True, was_read=False)
        assert finding is not None
        assert finding.kind == "write"
        assert finding.path == "/Users/x/.appconfig.toml"

    def test_passes_when_config_was_read(self) -> None:
        assert core.find_blind_write("/Users/x/.appconfig.toml", exists=True, was_read=True) is None

    def test_passes_when_creating_a_new_config(self) -> None:
        # No existing content to discard → creating, not overwriting.
        assert core.find_blind_write("/Users/x/.appconfig.toml", exists=False, was_read=False) is None

    def test_passes_on_non_config_file(self) -> None:
        assert core.find_blind_write("/repo/src/module.py", exists=True, was_read=False) is None


class TestFindBlindGitRestore:
    def test_fires_on_git_checkout_of_unread_config(self) -> None:
        finding = core.find_blind_git_restore(
            "git checkout -- ~/.appconfig.toml",
            was_read=lambda _p: False,
        )
        assert finding is not None
        assert finding.kind == "git-restore"

    def test_fires_on_git_restore_of_unread_config(self) -> None:
        finding = core.find_blind_git_restore(
            "git restore .config/app/config.toml",
            was_read=lambda _p: False,
        )
        assert finding is not None

    def test_passes_when_config_was_read(self) -> None:
        assert (
            core.find_blind_git_restore(
                "git checkout -- ~/.appconfig.toml",
                was_read=lambda _p: True,
            )
            is None
        )

    def test_passes_on_non_restore_git_command(self) -> None:
        assert core.find_blind_git_restore("git status", was_read=lambda _p: False) is None
        assert core.find_blind_git_restore("git log .appconfig.toml", was_read=lambda _p: False) is None

    def test_passes_on_restore_of_non_config_file(self) -> None:
        assert core.find_blind_git_restore("git checkout -- src/module.py", was_read=lambda _p: False) is None


class TestDenyReason:
    def test_write_reason_names_the_path_and_read_first(self) -> None:
        msg = core.deny_reason(core.ConfigOverwriteFinding(path="/Users/x/.appconfig.toml", kind="write"))
        assert "/Users/x/.appconfig.toml" in msg
        assert "Read" in msg

    def test_git_restore_reason_mentions_uncommitted(self) -> None:
        msg = core.deny_reason(core.ConfigOverwriteFinding(path="~/.appconfig.toml", kind="git-restore"))
        assert "uncommitted" in msg.lower()
        assert "config-overwrite-ok" in msg


# ── Live hook handler — the router-integration RED/GREEN proof ──────


import hooks.scripts.config_overwrite_guard as gate  # noqa: E402
import hooks.scripts.hook_router as router  # noqa: E402


def _capture(data: dict) -> tuple[bool, dict | None]:
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = gate.handle_block_config_overwrite(data)
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _seed_reads(state_dir: Path, session_id: str, read_paths: list[str]) -> None:
    """Write a ``<session>.reads`` file the way ``handle_read_dedup`` does."""
    reads_file = state_dir / f"{session_id}.reads"
    reads_file.write_text(
        "\n".join(f"0.0\t{p}" for p in read_paths) + "\n",
        encoding="utf-8",
    )


class TestHandleBlockConfigOverwrite:
    def _write_input(self, session_id: str, file_path: str) -> dict:
        return {
            "tool_name": "Write",
            "session_id": session_id,
            "tool_input": {"file_path": file_path, "content": "new = true\n"},
        }

    def _edit_input(self, session_id: str, file_path: str) -> dict:
        return {
            "tool_name": "Edit",
            "session_id": session_id,
            "tool_input": {"file_path": file_path, "old_string": "old = true", "new_string": "new = true"},
        }

    def test_red_blind_edit_over_unread_existing_config_is_denied(self) -> None:
        # An Edit overwrites from an old_string the agent may have assumed
        # rather than read — same risk as a blind Write.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, payload = _capture(self._edit_input("sess-edit-1", str(cfg)))
        assert blocked is True
        assert payload is not None

    def test_green_edit_after_read_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            _seed_reads(state_dir, "sess-edit-2", [str(cfg)])
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, _ = _capture(self._edit_input("sess-edit-2", str(cfg)))
        assert blocked is False

    def test_red_blind_write_over_unread_existing_config_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                # No reads file → the config was never read this session.
                blocked, payload = _capture(self._write_input("sess-1", str(cfg)))
        assert blocked is True
        assert payload is not None

    def test_green_write_after_read_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            _seed_reads(state_dir, "sess-2", [str(cfg)])
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, _ = _capture(self._write_input("sess-2", str(cfg)))
        assert blocked is False

    def test_green_creating_a_new_config_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"  # does NOT exist
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, _ = _capture(self._write_input("sess-3", str(cfg)))
        assert blocked is False

    def test_green_non_config_write_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "module.py"
            src.write_text("x = 1\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, _ = _capture(self._write_input("sess-4", str(src)))
        assert blocked is False

    def test_red_git_restore_of_unread_config_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            data = {
                "tool_name": "Bash",
                "session_id": "sess-5",
                "tool_input": {"command": f"git checkout -- {cfg}"},
            }
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, payload = _capture(data)
        assert blocked is True
        assert payload is not None

    def test_green_git_restore_after_read_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            _seed_reads(state_dir, "sess-6", [str(cfg)])
            data = {
                "tool_name": "Bash",
                "session_id": "sess-6",
                "tool_input": {"command": f"git checkout -- {cfg}"},
            }
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, _ = _capture(data)
        assert blocked is False

    def test_per_call_token_allows_a_blind_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            data = {
                "tool_name": "Write",
                "session_id": "sess-7",
                "tool_input": {
                    "file_path": str(cfg),
                    "content": "new = true\n[config-overwrite-ok: regenerating from template]\n",
                },
            }
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
                patch("sys.stderr", StringIO()),
            ):
                blocked, _ = _capture(data)
        assert blocked is False

    def test_kill_switch_disables_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".appconfig.toml"
            cfg.write_text("old = true\n", encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=False),
            ):
                blocked, _ = _capture(self._write_input("sess-8", str(cfg)))
        assert blocked is False


class TestSymlinkReadMatchesTargetOverwrite:
    """A Read of the symlink path satisfies an overwrite of its resolved target.

    The dotfile-symlink incident: the file is a symlink into the dotfiles
    repo. Reading it via the symlink path must clear a restore/overwrite
    expressed against the resolved target (and vice-versa) — the normalisation
    closure makes the membership test symmetric.
    """

    def test_read_via_symlink_clears_write_to_resolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dotfiles" / ".appconfig.toml"
            target.parent.mkdir()
            target.write_text("old = true\n", encoding="utf-8")
            link = Path(tmp) / ".appconfig.toml"
            link.symlink_to(target)
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            # Agent Read the symlink path; Write targets the same symlink path.
            _seed_reads(state_dir, "sess-9", [str(link)])
            data = {
                "tool_name": "Write",
                "session_id": "sess-9",
                "tool_input": {"file_path": str(target), "content": "new = true\n"},
            }
            with (
                patch.object(router, "STATE_DIR", state_dir),
                patch.object(gate, "_config_overwrite_gate_enabled", return_value=True),
            ):
                blocked, _ = _capture(data)
        assert blocked is False
