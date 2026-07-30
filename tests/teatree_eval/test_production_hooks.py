"""Production-hook plugin wiring — sandbox env pinning and hook-event detection.

The pure plugin-wiring seams live in :mod:`teatree.eval.production_hooks`; the
runner-side composition (``build_sdk_options`` registering the plugin, the
fail-loud ``hooks_not_registered`` guard) is tested with the runner in
``test_api_runner.py``.
"""

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import HookEventMessage

from teatree.eval.production_hooks import has_hook_events, hooked_env, t3_plugin, teatree_root


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


class TestHookedEnv:
    """`hooked_env` pins the loop/hook state roots inside the sandbox home."""

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
