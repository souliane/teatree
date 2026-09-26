"""Codex receives only tool and MCP policy it can enforce."""

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.codex_app_server_options import (
    CodexAppServerError,
    CodexAppServerOptions,
    codex_phase_policy_unavailable_reason,
)


def test_write_denial_becomes_codex_read_only_policy() -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(permission_mode="dontAsk", disallowed_tools=["Write", "Edit"])
    )

    assert options.sandbox_mode == "read-only"
    assert options.sandbox_policy == {"type": "readOnly"}


def test_bypass_permissions_stays_workspace_scoped_and_maps_all_workspace_roots() -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(
            cwd="/work/main",
            add_dirs=["/work/peer", "/work/main", "/work/docs"],
            permission_mode="bypassPermissions",
        )
    )

    assert options.core.add_dirs == ("/work/peer", "/work/main", "/work/docs")
    assert options.runtime_workspace_roots == ("/work/main", "/work/peer", "/work/docs")
    assert options.sandbox_mode == "workspace-write"
    assert options.sandbox_policy == {
        "type": "workspaceWrite",
        "writableRoots": ["/work/main", "/work/peer", "/work/docs"],
    }


def test_review_policy_is_read_only_and_disables_codex_multi_agent() -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            disallowed_tools=["Write", "Edit", "NotebookEdit", "Agent", "Task"],
        )
    )

    assert options.sandbox_mode == "read-only"
    assert options.sandbox_policy == {"type": "readOnly"}
    assert options.config == {"features": {"multi_agent": False}}


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Bash", "BashOutput", "KillBash", "KillShell"])
def test_phase_policy_names_codex_unenforceable_denials(tool: str) -> None:
    reason = codex_phase_policy_unavailable_reason([tool])

    assert reason is not None
    assert tool in reason


@pytest.mark.parametrize(
    "options",
    [
        ClaudeAgentOptions(allowed_tools=["Bash"]),
        ClaudeAgentOptions(disallowed_tools=["Read"]),
        ClaudeAgentOptions(disallowed_tools=["Bash"]),
        ClaudeAgentOptions(mcp_servers={"remote": {"type": "http", "url": "https://example.test"}}),
        ClaudeAgentOptions(output_format={"type": "json_schema", "schema": {"type": "object"}}),
    ],
)
def test_unsupported_tool_policy_is_rejected_before_spawn(options: ClaudeAgentOptions) -> None:
    with pytest.raises(CodexAppServerError, match="unsupported"):
        CodexAppServerOptions.from_sdk_options(options)
