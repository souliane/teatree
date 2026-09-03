"""Production-hook plugin wiring — sandbox env pinning and hook-event detection.

The pure plugin-wiring seams live in :mod:`teatree.eval.production_hooks`; the
runner-side composition (``build_sdk_options`` registering the plugin, the
fail-loud ``hooks_not_registered`` guard) is tested with the runner in
``test_api_runner.py``.
"""

import json
from pathlib import Path
from unittest import mock

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import HookEventMessage

from teatree.eval.production_hooks import (
    PluginRootUnresolvedError,
    has_hook_events,
    hook_script_paths,
    hooked_env,
    preflighted_plugin_root,
    t3_plugin,
    teatree_root,
)


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="s1",
        total_cost_usd=0.0123,
        usage=None,
        model_usage=None,
        result="ok",
    )


def _hook_response(hook_event: str) -> HookEventMessage:
    return HookEventMessage(
        subtype="hook_response",
        hook_event_name=hook_event,
        data={"hook_event": hook_event, "outcome": "", "output": "", "exit_code": 0},
    )


class TestT3Plugin:
    def test_registers_the_repo_root_as_a_local_plugin(self) -> None:
        assert t3_plugin() == {"type": "local", "path": str(teatree_root())}

    def test_teatree_root_is_the_repo_root(self) -> None:
        root = teatree_root()
        assert (root / "src" / "teatree").is_dir()
        assert (root / "hooks" / "hooks.json").is_file()


class TestPluginRootPreflight:
    """`${CLAUDE_PLUGIN_ROOT}` must expand to hook commands that actually run."""

    def test_shipped_manifest_resolves_against_the_repo_root(self) -> None:
        root = preflighted_plugin_root()
        assert root == teatree_root()
        assert hook_script_paths(root)
        assert all(path.is_file() for path in hook_script_paths(root))

    def test_raises_when_the_placeholder_never_expands(self, tmp_path: Path) -> None:
        # A manifest spelling the variable in a form the substitution misses leaves the
        # literal in the command — the exact shape that killed every gate at
        # `Hook cancelled` and reported six behavioural reds for one wiring fault.
        manifest = tmp_path / "hooks" / "hooks.json"
        manifest.parent.mkdir(parents=True)
        command = "$CLAUDE_PLUGIN_ROOT/hooks/scripts/run-hook.sh"
        manifest.write_text(
            json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": command}]}]}}),
            encoding="utf-8",
        )
        with (
            mock.patch("teatree.eval.production_hooks.teatree_root", return_value=tmp_path),
            pytest.raises(PluginRootUnresolvedError, match=r"run-hook\.sh"),
        ):
            preflighted_plugin_root()


class TestHookedEnv:
    """`hooked_env` pins the loop/hook state roots inside the sandbox home."""

    def test_resolves_the_plugin_root_to_an_absolute_path(self) -> None:
        env = hooked_env({"PATH": "/usr/bin"}, "/sandbox/home")
        assert env["CLAUDE_PLUGIN_ROOT"] == str(teatree_root())
        assert "${" not in env["CLAUDE_PLUGIN_ROOT"]

    def test_redirects_all_state_roots_into_the_sandbox_home(self) -> None:
        env = hooked_env({"PATH": "/usr/bin", "XDG_DATA_HOME": "/real/user/data"}, "/sandbox/home")
        assert env["XDG_DATA_HOME"] == "/sandbox/home/.local/share"
        assert env["T3_LOOP_REGISTRY_DIR"] == "/sandbox/home/loop-registry"
        assert env["T3_HOOK_STATE_DIR"] == "/sandbox/home/hook-state"
        assert env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] == "/sandbox/home/statusline-state"
        # A developer's real XDG_DATA_HOME never survives into a hooked child.
        assert env["XDG_DATA_HOME"] != "/real/user/data"

    def test_does_not_mutate_the_input_env(self) -> None:
        original = {"PATH": "/usr/bin"}
        hooked_env(original, "/sandbox/home")
        assert "XDG_DATA_HOME" not in original


class TestHasHookEvents:
    def test_true_when_any_hook_event_present(self) -> None:
        assert has_hook_events([_result(), _hook_response("PreToolUse")]) is True

    def test_false_when_no_hook_event(self) -> None:
        assert has_hook_events([_result()]) is False
