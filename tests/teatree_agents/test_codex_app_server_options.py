"""Codex receives only tool and MCP policy it can enforce."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_options
from teatree.agents.codex_app_server_options import (
    CodexAppServerError,
    CodexAppServerOptions,
    codex_phase_policy_unavailable_reason,
    codex_worker_denials,
)
from teatree.agents.sdk_tool_map import sdk_disallowed_tools_for_phase
from tests.teatree_agents._codex_plugin import CODEX_PLUGIN_ID
from tests.teatree_agents._executor_fakes import EXECUTOR_ID, FakeExecutor, attested


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


@pytest.fixture
def on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: False)


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


@pytest.mark.usefixtures("in_container")
@pytest.mark.parametrize("phase", ["bughunt", "dogfood_smoke"])
def test_container_execution_phase_lifts_only_the_mutation_denials(phase: str) -> None:
    denied = sdk_disallowed_tools_for_phase(phase)
    lifted = codex_worker_denials(denied, phase)

    assert codex_phase_policy_unavailable_reason(denied) is not None
    assert codex_phase_policy_unavailable_reason(lifted) is None
    assert set(denied) - set(lifted) == {"Edit", "NotebookEdit", "Write"}


@pytest.mark.usefixtures("in_container")
def test_container_read_only_phase_keeps_every_denial() -> None:
    denied = sdk_disallowed_tools_for_phase("reviewing")

    assert codex_worker_denials(denied, "reviewing") == denied


@pytest.mark.usefixtures("on_host")
@pytest.mark.parametrize("phase", ["bughunt", "dogfood_smoke"])
def test_host_execution_phase_keeps_every_denial(phase: str) -> None:
    denied = sdk_disallowed_tools_for_phase(phase)

    assert codex_worker_denials(denied, phase) == denied


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


_PLANNING_OPTIONS = ClaudeAgentOptions(
    cwd="/work/main", permission_mode="bypassPermissions", disallowed_tools=["Write", "Edit", "NotebookEdit"]
)


@pytest.mark.parametrize(
    "impostor", [object(), SimpleNamespace(container_id=EXECUTOR_ID)], ids=["object", "look-alike"]
)
def test_mutation_denied_phase_needs_an_attestation(monkeypatch: pytest.MonkeyPatch, impostor: object) -> None:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)

    with pytest.raises(CodexAppServerError, match="read-only inside a container"):
        CodexAppServerOptions.from_sdk_options(_PLANNING_OPTIONS, executor=impostor)


@pytest.mark.usefixtures("pass_store")
def test_an_attested_mutation_denied_phase_runs_unsandboxed_in_the_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attestation = attested(FakeExecutor(tmp_path), monkeypatch)
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)

    options = CodexAppServerOptions.from_sdk_options(_PLANNING_OPTIONS, executor=attestation)

    assert (options.sandbox_mode, options.sandbox_policy) == ("danger-full-access", {"type": "dangerFullAccess"})
    assert options.config["projects"] == {"/work/main": {"trust_level": "untrusted"}}
    assert options.config["plugins"] == {CODEX_PLUGIN_ID: {"enabled": False}}
    assert options.executor is attestation


@pytest.mark.usefixtures("pass_store")
def test_an_attested_shell_denied_phase_uses_the_executor_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attestation = attested(FakeExecutor(tmp_path), monkeypatch)
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    denied = ["Write", "Edit", "NotebookEdit", "Bash", "BashOutput", "KillBash", "KillShell"]

    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions", disallowed_tools=denied),
        executor=attestation,
    )

    assert options.sandbox_mode == "danger-full-access"
    assert options.executor is attestation

    with pytest.raises(CodexAppServerError, match="cannot enforce tool denials for: Bash"):
        CodexAppServerOptions.from_sdk_options(
            ClaudeAgentOptions(cwd="/work/main", permission_mode="bypassPermissions", disallowed_tools=denied)
        )
