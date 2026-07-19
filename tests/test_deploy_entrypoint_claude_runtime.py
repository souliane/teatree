# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""Every agent-spawning role provisions the per-container Claude runtime.

The agent's skills ship as the ``t3@souliane`` Claude Code plugin, registered by
``t3 setup`` (PluginRegistrar) into ``~/.claude`` — which is PER-CONTAINER ephemeral
(docker-compose.yml bind-mounts only ``~/.claude/projects``). So init's registration
never reaches the worker/admin/slack-listener containers, and their agents load ZERO
skills unless each re-runs the claude-env prep itself. This asserts the entrypoint's
``prepare_claude_runtime`` (seed settings + ``t3 setup``) runs in EVERY role that
spawns ``claude`` — init, worker, admin, slack-listener — not only init.

A structural (text) test over ``deploy/entrypoint.sh`` — the role dispatch is a
shell ``case``, so the contract is which branches invoke the prep.
"""

import re
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"


def _role_branch_body(source: str, role: str) -> str:
    """Return the shell between ``<role>)`` and its terminating ``;;`` in the ROLE case."""
    case_start = source.index('case "$ROLE" in')
    body = source[case_start:]
    match = re.search(rf"(?m)^{re.escape(role)}\)\n(.*?)^\s*;;", body, re.DOTALL)
    assert match is not None, f"role branch {role!r} not found in the ROLE case"
    return match.group(1)


class TestPrepareClaudeRuntime:
    def test_prepare_function_seeds_settings_and_runs_setup(self) -> None:
        source = ENTRYPOINT.read_text(encoding="utf-8")
        match = re.search(r"(?m)^prepare_claude_runtime\(\) \{\n(.*?)^\}", source, re.DOTALL)
        assert match is not None, "prepare_claude_runtime() not defined"
        body = match.group(1)
        assert "seed_claude_settings" in body
        assert "t3 setup" in body

    def test_every_agent_spawning_role_prepares_the_claude_runtime(self) -> None:
        source = ENTRYPOINT.read_text(encoding="utf-8")
        for role in ("init", "worker", "admin", "slack-listener"):
            assert "prepare_claude_runtime" in _role_branch_body(source, role), (
                f"role {role!r} must run prepare_claude_runtime so its agent's ~/.claude has the t3 plugin/skills"
            )

    def test_runtime_roles_prepare_before_their_exec(self) -> None:
        # The prep must land BEFORE the role hands off to its long-running process.
        source = ENTRYPOINT.read_text(encoding="utf-8")
        for role, exec_marker in (
            ("worker", "exec t3 worker"),
            ("admin", "exec t3 admin"),
            ("slack-listener", "exec t3 slack listen"),
        ):
            body = _role_branch_body(source, role)
            # rindex: the exec command is the LAST occurrence (a comment may name it too).
            assert body.index("prepare_claude_runtime") < body.rindex(exec_marker)
