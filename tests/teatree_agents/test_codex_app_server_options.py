"""Codex receives only tool and MCP policy it can enforce."""

from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_options
from teatree.agents.codex_app_server_options import (
    CONTAINER_IS_SANDBOX_ENV,
    CodexAppServerError,
    CodexAppServerOptions,
    codex_phase_policy_unavailable_reason,
)
from teatree.utils.ports import running_in_container
from tests.teatree_agents._codex_plugin import CODEX_PLUGIN_ID


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
    assert options.config == {"features": {"apps": False, "multi_agent": False}}


@pytest.mark.parametrize(
    ("disallowed_tools", "features"),
    [([], {"apps": False}), (["Agent", "Task"], {"apps": False, "multi_agent": False})],
    ids=["no-denial", "dispatch-denied"],
)
def test_every_thread_switches_off_chatgpt_apps(disallowed_tools: list[str], features: dict[str, bool]) -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions", disallowed_tools=disallowed_tools)
    )

    assert options.config["features"] == features


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Bash", "BashOutput", "KillBash", "KillShell"])
def test_phase_policy_names_codex_unenforceable_denials(tool: str) -> None:
    reason = codex_phase_policy_unavailable_reason([tool])

    assert reason is not None
    assert tool in reason


@pytest.fixture
def in_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)


@pytest.mark.parametrize(
    ("marker", "role", "opt_in", "expected"),
    [
        (False, None, None, False),
        (False, "worker", "1", False),
        (True, "worker", None, False),
        (True, "admin", None, False),
        (True, None, "1", True),
        (True, "worker", "1", True),
        (True, "worker", "0", False),
        (True, "worker", "true", False),
        (True, "worker", "", False),
    ],
    ids=[
        "host",
        "host-with-the-opt-in-leaked",
        "core-compose-worker",
        "admin",
        "opt-in",
        "opted-in-worker",
        "opt-in-off",
        "opt-in-not-one",
        "opt-in-empty",
    ],
)
def test_container_is_the_sandbox_only_on_an_explicit_opt_in_inside_a_container(
    monkeypatch: pytest.MonkeyPatch, *, marker: bool, role: str | None, opt_in: str | None, expected: bool
) -> None:
    monkeypatch.setattr(codex_app_server_options, "running_in_container", running_in_container)
    monkeypatch.setattr(Path, "exists", lambda _path: marker)
    monkeypatch.setattr(Path, "read_text", lambda _path, **_kwargs: "0::/user.slice\n")
    for name, value in (("TEATREE_ROLE", role), (CONTAINER_IS_SANDBOX_ENV, opt_in)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    codex_app_server_options.container_is_the_sandbox.cache_clear()
    try:
        assert codex_app_server_options.container_is_the_sandbox() is expected
    finally:
        codex_app_server_options.container_is_the_sandbox.cache_clear()


@pytest.mark.usefixtures("in_container")
def test_container_write_phase_runs_codex_without_its_own_sandbox() -> None:
    # The container is the sandbox; Codex's bwrap cannot create a user namespace inside it.
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions")
    )

    assert options.sandbox_mode == "danger-full-access"
    assert options.sandbox_policy == {"type": "dangerFullAccess"}


@pytest.mark.usefixtures("in_container")
@pytest.mark.parametrize(
    "options",
    [ClaudeAgentOptions(permission_mode="plan"), ClaudeAgentOptions(disallowed_tools=["Write", "Edit"])],
)
def test_container_refuses_read_only_it_cannot_enforce(options: ClaudeAgentOptions) -> None:
    with pytest.raises(CodexAppServerError, match=r"unsupported.*container"):
        CodexAppServerOptions.from_sdk_options(options)


@pytest.mark.usefixtures("in_container")
def test_container_phase_policy_routes_read_only_phases_to_another_harness() -> None:
    reason = codex_phase_policy_unavailable_reason(["Edit", "NotebookEdit", "Write"])

    assert reason is not None
    assert "container" in reason


def test_host_phase_policy_still_enforces_read_only_through_the_sandbox() -> None:
    assert codex_phase_policy_unavailable_reason(["Edit", "NotebookEdit", "Write"]) is None


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


@pytest.mark.usefixtures("in_container")
def test_worker_write_session_never_trusts_a_project_codex_layer() -> None:
    # A trusted project's `.codex/rules` ALLOW would let a command skip the approval gate.
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", add_dirs=["/work/peer"], permission_mode="bypassPermissions")
    )

    assert options.config["projects"] == {
        "/work/main": {"trust_level": "untrusted"},
        "/work/peer": {"trust_level": "untrusted"},
    }


def test_host_session_leaves_project_trust_and_plugins_to_codex() -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions")
    )

    assert "projects" not in options.config
    assert "plugins" not in options.config


@pytest.mark.usefixtures("in_container")
def test_worker_write_session_switches_off_the_teatree_plugin_and_its_mcp_server() -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions")
    )

    assert options.config["plugins"] == {CODEX_PLUGIN_ID: {"enabled": False}}


@pytest.mark.usefixtures("in_container")
def test_worker_write_session_keeps_apps_off_beside_its_trust_and_plugin_overrides() -> None:
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions")
    )

    assert options.config == {
        "features": {"apps": False},
        "projects": {"/work/main": {"trust_level": "untrusted"}},
        "plugins": {CODEX_PLUGIN_ID: {"enabled": False}},
    }
