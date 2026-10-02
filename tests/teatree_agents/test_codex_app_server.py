"""Codex App Server JSONL is translated at the real subprocess boundary."""

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from teatree.agents import harness_registry
from teatree.agents.codex_app_server import (
    CODEX_APP_SERVER_CAPABILITIES,
    CodexAppServerError,
    CodexAppServerHarness,
    CodexAppServerOptions,
    CodexAppServerSession,
    codex_app_server_spec,
    codex_process_env,
)
from teatree.agents.codex_app_server_messages import tool_blocks, translate_usage
from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY, CodexAuthCache, CodexAuthCacheError
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessFallbackError,
    HarnessFallbackKind,
    HarnessSpec,
    register_harness,
    select_harness,
)

_THREAD_ID = "0197e1d4-1f5f-7b00-8000-000000000001"
_PROTOCOL_CONTRACT = Path(__file__).parents[1] / "fixtures" / "codex_app_server" / "0.155.1-contract.json"
_FAKE_SERVER = r"""
import json
import os
import sys

scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")
log_path = os.environ["FAKE_CODEX_LOG"]
with open(log_path + ".env", "w", encoding="utf-8") as env_log:
    json.dump({"CODEX_HOME": os.environ.get("CODEX_HOME"), "HOME": os.environ.get("HOME")}, env_log)
turn_count = 0
experimental_api = False

def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

def refuse_ungated_experimental_field(method, request_id, params):
    # Mirrors the real server: an experimental field is refused unless initialize negotiated it.
    if "runtimeWorkspaceRoots" in params and not experimental_api:
        emit({
            "id": request_id,
            "error": {"code": -32600, "message": f"{method}.runtimeWorkspaceRoots requires experimentalApi capability"},
        })
        return True
    return False

if scenario == "stderr_flood":
    sys.stderr.write("x" * 200000)
    sys.stderr.flush()

for raw in sys.stdin:
    request = json.loads(raw)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    method = request["method"]
    request_id = request.get("id")
    if method == "initialized":
        continue
    if method == "initialize":
        if scenario == "initialize_hang":
            continue
        if scenario == "protocol_error":
            sys.stdout.write("not-json do-not-print\n")
            sys.stdout.flush()
            continue
        if scenario == "initialize_error":
            emit({"id": request_id, "error": {"code": -32000, "message": "do-not-print"}})
            continue
        experimental_api = request["params"].get("capabilities", {}).get("experimentalApi") is True
        emit({"id": request_id, "result": {"userAgent": "fake-codex"}})
        if scenario == "unexpected_server_request":
            emit({
                "id": 900,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "do-not-print"},
            })
        continue
    if method == "account/read":
        if scenario == "account_unauthorized":
            emit({
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": "do-not-print",
                    "data": {"codexErrorInfo": "unauthorized"},
                },
            })
            continue
        account_type = "apiKey" if scenario == "api_key" else "chatgpt"
        account = {"type": account_type}
        if account_type == "chatgpt":
            account |= {"email": "operator@example.test", "planType": "pro"}
        emit({"id": request_id, "result": {"account": account, "requiresOpenaiAuth": True}})
        continue
    if method in {"thread/start", "thread/resume", "turn/start"} and refuse_ungated_experimental_field(
        method, request_id, request["params"]
    ):
        continue
    if method in {"thread/start", "thread/resume"}:
        emit({
            "id": request_id,
            "result": {
                "thread": {"id": "0197e1d4-1f5f-7b00-8000-000000000001", "turns": []},
                "model": "gpt-5.6-sol",
            },
        })
        continue
    if method == "turn/start":
        turn_count += 1
        if scenario == "second_turn_start_quota" and turn_count == 2:
            emit({
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": "do-not-print",
                    "data": {"codexErrorInfo": "usageLimitExceeded"},
                },
            })
            continue
        turn = {"id": "turn-1", "items": [], "status": "inProgress"}
        emit({"id": request_id, "result": {"turn": turn}})
        if scenario == "interrupt":
            continue
        if scenario == "stream_disconnect_after_turn":
            sys.exit(0)
        common = {"threadId": "0197e1d4-1f5f-7b00-8000-000000000001", "turnId": "turn-1"}
        early_errors = {
                "turn_error_quota": "usageLimitExceeded",
                "turn_error_access": "cyberPolicy",
                "turn_error_5xx": {
                    "responseStreamConnectionFailed": {"httpStatusCode": 503}
                },
                "turn_error_bad_request": "badRequest",
        }
        if scenario in early_errors:
            info = early_errors[scenario]
            emit({
                "method": "turn/completed",
                "params": common | {
                    "turn": {
                        "id": "turn-1",
                        "items": [],
                        "status": "failed",
                        "error": {"message": "do-not-print", "codexErrorInfo": info},
                    }
                },
            })
            continue
        emit({"method": "item/agentMessage/delta", "params": common | {"itemId": "msg-1", "delta": "good"}})
        emit({"method": "item/agentMessage/delta", "params": common | {"itemId": "msg-1", "delta": "bye"}})
        emit({
            "method": "item/completed",
            "params": common | {
                "completedAtMs": 1,
                "item": {"id": "msg-1", "type": "agentMessage", "text": "goodbye"},
            },
        })
        if scenario in {"collab_success", "turn_error_quota_after_collab"}:
            collab = {
                "id": "collab-1",
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "senderThreadId": common["threadId"],
                "receiverThreadIds": ["child-1"],
                "model": "gpt-5.6-sol",
                "reasoningEffort": "high",
                "agentsStates": {"child-1": {"status": "running"}},
                "status": "inProgress",
            }
            emit({"method": "item/started", "params": common | {"item": collab}})
            if scenario == "turn_error_quota_after_collab":
                emit({
                    "method": "turn/completed",
                    "params": common | {
                        "turn": {
                            "id": "turn-1",
                            "items": [],
                            "status": "failed",
                            "error": {"message": "do-not-print", "codexErrorInfo": "usageLimitExceeded"},
                        }
                    },
                })
                continue
            collab["status"] = "completed"
            collab["agentsStates"] = {"child-1": {"status": "completed"}}
            emit({"method": "item/completed", "params": common | {"item": collab}})
            emit({
                "method": "item/completed",
                "params": common | {
                    "item": {
                        "id": "activity-1",
                        "type": "subAgentActivity",
                        "agentThreadId": "child-1",
                        "agentPath": "coder",
                        "kind": "completed",
                    }
                },
            })
            emit({
                "method": "turn/completed",
                "params": common | {
                    "turn": {"id": "turn-1", "items": [], "status": "completed", "durationMs": 12}
                },
            })
            continue
        if scenario == "turn_error_quota_after_tool":
            emit({
                "method": "item/started",
                "params": common | {
                    "item": {
                        "id": "tool-started",
                        "type": "commandExecution",
                        "command": "pwd",
                        "cwd": "/work",
                        "status": "inProgress",
                        "commandActions": [],
                    },
                },
            })
            emit({
                "method": "turn/completed",
                "params": common | {
                    "turn": {
                        "id": "turn-1",
                        "items": [],
                        "status": "failed",
                        "error": {
                            "message": "do-not-print",
                            "codexErrorInfo": "usageLimitExceeded",
                        },
                    }
                },
            })
            continue
        emit({
            "method": "item/completed",
            "params": common | {
                "completedAtMs": 3,
                "item": {
                    "id": "mcp-1",
                    "type": "mcpToolCall",
                    "server": "teatree",
                    "tool": "status",
                    "arguments": {"task": 42},
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "ready"}]},
                    "error": None,
                },
            },
        })
        emit({
            "method": "item/completed",
            "params": common | {
                "completedAtMs": 2,
                "item": {
                    "id": "tool-1",
                    "type": "commandExecution",
                    "command": "pwd",
                    "cwd": "/work",
                    "status": "completed",
                    "commandActions": [],
                    "aggregatedOutput": "/work",
                    "exitCode": 0,
                },
            },
        })
        emit({
            "method": "thread/tokenUsage/updated",
            "params": common | {
                "tokenUsage": {
                    "last": {
                        "inputTokens": 10, "cachedInputTokens": 2, "cacheWriteInputTokens": 1,
                        "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": 13,
                    },
                    "total": {
                        "inputTokens": 10, "cachedInputTokens": 2, "cacheWriteInputTokens": 1,
                        "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": 13,
                    },
                    "modelContextWindow": 1000,
                },
            },
        })
        emit({
            "method": "turn/completed",
            "params": common | {"turn": {"id": "turn-1", "items": [], "status": "completed", "durationMs": 12}},
        })
        continue
    if method == "turn/interrupt":
        emit({"id": request_id, "result": {}})
        common = {"threadId": "0197e1d4-1f5f-7b00-8000-000000000001", "turnId": "turn-1"}
        emit({
            "method": "turn/completed",
            "params": common | {"turn": {"id": "turn-1", "items": [], "status": "interrupted", "durationMs": 1}},
        })
"""


@pytest.fixture
def fake_codex(tmp_path: Path) -> tuple[tuple[str, ...], Path]:
    server = tmp_path / "fake_codex.py"
    server.write_text(textwrap.dedent(_FAKE_SERVER), encoding="utf-8")
    return (sys.executable, str(server)), tmp_path / "requests.jsonl"


def _options(
    log: Path,
    *,
    scenario: str = "success",
    resume: str | None = None,
    model: str | None = "gpt-5.6-sol",
) -> tuple[CodexAppServerOptions, str | None]:
    sdk = ClaudeAgentOptions(
        model=model,
        effort="high",
        system_prompt="system context",
        cwd="/work",
        add_dirs=["/peer"],
        permission_mode="bypassPermissions",
        disallowed_tools=["AskUserQuestion", "WebSearch", "Agent", "Task"],
        strict_mcp_config=True,
        mcp_servers={
            "teatree": {
                "type": "stdio",
                "command": "t3",
                "args": ["mcp", "serve"],
                "env": {"T3_DATA_DIR": "/data"},
            }
        },
        env={"FAKE_CODEX_LOG": str(log), "FAKE_CODEX_SCENARIO": scenario},
        resume=resume,
    )
    return CodexAppServerOptions.from_sdk_options(sdk), sdk.resume


def _session(
    options: CodexAppServerOptions,
    *,
    resume: str | None,
    code_home: Path,
    command: tuple[str, ...],
) -> CodexAppServerSession:
    return CodexAppServerSession(
        options,
        resume=resume,
        code_home=code_home,
        command=command,
        process_env=options.core.env,
    )


def _requests(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _child_env(log: Path) -> dict[str, str]:
    return json.loads(log.with_suffix(log.suffix + ".env").read_text(encoding="utf-8"))


def _assert_pinned_protocol_contract(requests: list[dict[str, object]]) -> None:
    contract = json.loads(_PROTOCOL_CONTRACT.read_text(encoding="utf-8"))
    assert contract["codex_cli_version"] == "0.155.1"
    for request in requests:
        method = request["method"]
        expected = contract["methods"][method]
        params = request.get("params", {})
        assert isinstance(params, dict)
        assert set(expected["required"]) <= params.keys()
        assert params.keys() <= set(expected["allowed"])
        if method in {"thread/start", "thread/resume"}:
            assert params["sandbox"] in contract["sandbox_modes"]


def test_capabilities_are_truthful_after_stdio_mcp_translation_is_covered() -> None:
    spec = codex_app_server_spec()

    assert CODEX_APP_SERVER_CAPABILITIES.mcp is True
    assert CODEX_APP_SERVER_CAPABILITIES.server_resume is True
    assert CODEX_APP_SERVER_CAPABILITIES.hooks is False
    assert CODEX_APP_SERVER_CAPABILITIES.structured_output is False
    assert CODEX_APP_SERVER_CAPABILITIES.managed_lane is True
    assert spec.allows_provider is False
    assert spec.valid_providers == frozenset()


def test_codex_process_env_is_private_and_drops_ambient_credentials(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    factory_home = tmp_path / "factory"
    factory_home.mkdir()

    env = codex_process_env(
        home,
        ambient={
            "HOME": str(factory_home),
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "GIT_AUTHOR_NAME": "Factory Agent",
            "GIT_AUTHOR_EMAIL": "factory@example.invalid",
            "T3_CONFIG_DB": "/control/db.sqlite3",
            "T3_CONTROL_DB_DIR": "/control",
            "T3_REPO": "/src/teatree",
            "PASSWORD_STORE_DIR": "/factory/pass",
            "GNUPGHOME": "/factory/gnupg",
            "XDG_DATA_HOME": "/factory/data",
            "XDG_CONFIG_HOME": "/factory/config",
            "GH_CONFIG_DIR": "/factory/gh",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
            "GITLAB_TOKEN": "gitlab-secret",
            "T3_SECRET_KEY": "django-secret",
            "T3_ADMIN_PASSWORD": "admin-secret",
        },
    )

    assert env == {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "GIT_AUTHOR_NAME": "Factory Agent",
        "GIT_AUTHOR_EMAIL": "factory@example.invalid",
        "GIT_COMMITTER_NAME": "Factory Agent",
        "GIT_COMMITTER_EMAIL": "factory@example.invalid",
        "T3_CONFIG_DB": "/control/db.sqlite3",
        "T3_CONTROL_DB_DIR": "/control",
        "T3_REPO": "/src/teatree",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "CODEX_HOME": str(home),
        "HOME": str(home),
    }
    assert (
        not {
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "GH_CONFIG_DIR",
            "GITLAB_TOKEN",
            "GNUPGHOME",
            "PASSWORD_STORE_DIR",
            "T3_SECRET_KEY",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
        & env.keys()
    )


def test_usage_records_only_the_latest_turn_on_a_resumed_thread() -> None:
    assert translate_usage(
        {
            "last": {"inputTokens": 10, "outputTokens": 3, "cachedInputTokens": 2, "cacheWriteInputTokens": 1},
            "total": {"inputTokens": 900, "outputTokens": 300, "cachedInputTokens": 200, "cacheWriteInputTokens": 100},
        }
    ) == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 1,
    }


@pytest.mark.parametrize("item_type", ["dynamicToolCall", "imageGeneration"])
def test_extended_tool_items_are_visible_and_make_replay_unsafe(tmp_path: Path, item_type: str) -> None:
    options, _resume = _options(tmp_path / "requests.jsonl")
    session = CodexAppServerSession(options, resume=None, code_home=tmp_path / "home")
    item = {
        "id": "side-effect-1",
        "type": item_type,
        "tool": "external-write",
        "arguments": {"target": "remote"},
        "prompt": "generate",
        "savedPath": "/work/generated.png",
        "status": "completed",
        "result": {"ok": True},
    }

    session._record_possible_side_effect(item)

    assert session._side_effects_started is True
    assert [block.id if isinstance(block, ToolUseBlock) else block.tool_use_id for block in tool_blocks(item)] == [
        "side-effect-1",
        "side-effect-1",
    ]


def test_default_code_home_is_private_teatree_data_not_ambient_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX_HOME", "/ambient/must-not-be-used")
    monkeypatch.delenv("T3_CODEX_HOME", raising=False)
    monkeypatch.setattr("teatree.agents.codex_auth_cache.data_dir_root", lambda: tmp_path / "data")

    assert CodexAppServerHarness().code_home == tmp_path / "data" / "codex-home"


def test_configured_private_home_overrides_ambient_codex_home_in_the_child(
    fake_codex: tuple[tuple[str, ...], Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command, log = fake_codex
    private_home = tmp_path / "factory-private-codex"
    ambient_home = tmp_path / "operator-home" / ".codex"
    monkeypatch.setenv("T3_CODEX_HOME", str(private_home))
    monkeypatch.setenv("CODEX_HOME", str(ambient_home))
    options, resume = _options(log)
    harness = CodexAppServerHarness(command=command)
    session = _session(options, resume=resume, code_home=harness.code_home, command=command)

    async def run() -> None:
        await session.start()
        await session.close()

    asyncio.run(run())

    assert harness.code_home == private_home
    assert _child_env(log)["CODEX_HOME"] == str(private_home)
    assert _child_env(log)["CODEX_HOME"] != str(ambient_home)


def test_missing_codex_binary_falls_through_to_the_next_configured_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_spec = codex_app_server_spec()
    monkeypatch.setattr("teatree.agents.codex_app_server.shutil.which", lambda _name: None)
    register_harness(
        HarnessSpec(
            name="codex_probe_test",
            factory=codex_spec.factory,
            unavailable_reason=codex_spec.unavailable_reason,
        )
    )
    register_harness(HarnessSpec(name="claude_probe_test", factory=codex_spec.factory))
    try:
        selection = select_harness(["codex_probe_test", "claude_probe_test"], HarnessBuildContext())
    finally:
        harness_registry._REGISTRY.pop("codex_probe_test", None)
        harness_registry._REGISTRY.pop("claude_probe_test", None)

    assert selection.spec.name == "claude_probe_test"
    assert selection.rejected[0].name == "codex_probe_test"
    assert "codex" in selection.rejected[0].reason.lower()


@pytest.mark.parametrize("phase", ["scoping", "requesting_review", "answering", "directive_reading"])
def test_unenforceable_phase_policy_falls_through_before_codex_starts(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    codex_spec = codex_app_server_spec()
    monkeypatch.setattr("teatree.agents.codex_app_server.shutil.which", lambda _name: "/usr/bin/codex")
    register_harness(
        HarnessSpec(
            name="codex_policy_probe_test",
            factory=codex_spec.factory,
            unavailable_reason=codex_spec.unavailable_reason,
        )
    )
    register_harness(HarnessSpec(name="claude_policy_probe_test", factory=codex_spec.factory))
    try:
        selection = select_harness(
            ["codex_policy_probe_test", "claude_policy_probe_test"],
            HarnessBuildContext(phase=phase),
        )
    finally:
        harness_registry._REGISTRY.pop("codex_policy_probe_test", None)
        harness_registry._REGISTRY.pop("claude_policy_probe_test", None)

    assert selection.spec.name == "claude_policy_probe_test"
    assert phase in selection.rejected[0].reason
    assert "cannot enforce" in selection.rejected[0].reason


@pytest.mark.parametrize(
    "cache_error",
    [
        CodexAuthCacheError.pass_read_failed(CODEX_AUTH_PASS_ENTRY),
        CodexAuthCacheError.invalid_base64(),
        CodexAuthCacheError.invalid_json(),
    ],
)
def test_auth_cache_bootstrap_failures_are_safe_typed_fallbacks(
    tmp_path: Path, cache_error: CodexAuthCacheError
) -> None:
    harness = CodexAppServerHarness(code_home=tmp_path / "home")

    async def run() -> None:
        with patch.object(CodexAuthCache, "hydrate", side_effect=cache_error):
            async with harness.open(ClaudeAgentOptions()):
                pytest.fail("auth failure must happen before Codex starts")

    with pytest.raises(HarnessFallbackError) as raised:
        asyncio.run(run())

    assert raised.value.kind is HarnessFallbackKind.AUTH
    assert CODEX_AUTH_PASS_ENTRY in str(raised.value)
    assert "t3 codex auth import" in str(raised.value)
    assert "do-not-print" not in str(raised.value)


def test_auth_persist_failure_after_completed_turn_retains_thread_and_forbids_replay(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    home = tmp_path / "home"
    harness = CodexAppServerHarness(code_home=home, command=command)
    options, _resume = _options(log)
    sdk_options = ClaudeAgentOptions(
        model=options.core.model,
        effort=options.core.effort,
        system_prompt=options.core.system_prompt,
        cwd=options.core.cwd,
        add_dirs=list(options.core.add_dirs),
        permission_mode="bypassPermissions",
        disallowed_tools=["Agent", "Task"],
        env=options.core.env,
    )

    async def run() -> None:
        with (
            patch.object(CodexAuthCache, "hydrate", return_value=home / "auth.json"),
            patch.object(CodexAuthCache, "persist", side_effect=CodexAuthCacheError.pass_persist_failed("entry")),
        ):
            async with harness.open(sdk_options) as session:
                await session.query("do the work")
                _ = [message async for message in session.receive_response()]

    with pytest.raises(HarnessFallbackError) as raised:
        asyncio.run(run())

    assert raised.value.kind is HarnessFallbackKind.AUTH
    assert raised.value.side_effects_started is True
    assert raised.value.agent_session_id == _THREAD_ID
    assert CODEX_AUTH_PASS_ENTRY in str(raised.value)
    assert "t3 codex auth import" in str(raised.value)
    assert "do-not-print" not in str(raised.value)


def test_start_query_and_stream_translate_the_exact_protocol(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log)
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> list[object]:
        await session.start()
        try:
            await session.query("do the work")
            return [message async for message in session.receive_response()]
        finally:
            await session.close()

    messages = asyncio.run(run())
    requests = _requests(log)
    _assert_pinned_protocol_contract(requests)

    assert [request["method"] for request in requests[:4]] == [
        "initialize",
        "initialized",
        "account/read",
        "thread/start",
    ]
    assert requests[0]["params"] == {
        "clientInfo": {"name": "teatree", "version": "0.0.1"},
        "capabilities": {"experimentalApi": True},
    }
    thread_params = requests[3]["params"]
    assert thread_params == {
        "cwd": "/work",
        "model": "gpt-5.6-sol",
        "developerInstructions": "system context",
        "approvalPolicy": "never",
        "sandbox": "workspace-write",
        "runtimeWorkspaceRoots": ["/work", "/peer"],
        "config": {
            "mcp_servers": {"teatree": {"command": "t3", "args": ["mcp", "serve"], "env": {"T3_DATA_DIR": "/data"}}},
            "web_search": "disabled",
            "features": {"multi_agent": False},
        },
    }
    assert requests[4]["params"] == {
        "threadId": _THREAD_ID,
        "input": [{"type": "text", "text": "do the work"}],
        "cwd": "/work",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": ["/work", "/peer"]},
        "runtimeWorkspaceRoots": ["/work", "/peer"],
    }
    assistant = [message for message in messages if isinstance(message, AssistantMessage)]
    text = [block.text for message in assistant for block in message.content if isinstance(block, TextBlock)]
    assert text == ["goodbye"]
    tools = [block for message in assistant for block in message.content if isinstance(block, ToolUseBlock)]
    assert [(tool.id, tool.name, tool.input) for tool in tools] == [
        ("mcp-1", "mcp__teatree__status", {"task": 42}),
        ("tool-1", "Bash", {"command": "pwd", "cwd": "/work"}),
    ]
    tool_results = [block for message in assistant for block in message.content if isinstance(block, ToolResultBlock)]
    assert [(block.tool_use_id, block.is_error) for block in tool_results] == [
        ("mcp-1", False),
        ("tool-1", False),
    ]
    result = next(message for message in messages if isinstance(message, ResultMessage))
    assert result.session_id == _THREAD_ID
    assert result.result == "goodbye"
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 1,
    }
    assert result.model_usage is not None
    assert list(result.model_usage) == ["gpt-5.6-sol"]


def test_resume_reapplies_current_thread_options(fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path) -> None:
    command, log = fake_codex
    options, _ = _options(log, resume=_THREAD_ID)
    session = _session(options, resume=_THREAD_ID, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        await session.start()
        await session.close()

    asyncio.run(run())

    resume = next(request for request in _requests(log) if request["method"] == "thread/resume")
    _assert_pinned_protocol_contract(_requests(log))
    assert resume["params"] == {
        "threadId": _THREAD_ID,
        "cwd": "/work",
        "model": "gpt-5.6-sol",
        "developerInstructions": "system context",
        "approvalPolicy": "never",
        "sandbox": "workspace-write",
        "runtimeWorkspaceRoots": ["/work", "/peer"],
        "config": {
            "mcp_servers": {"teatree": {"command": "t3", "args": ["mcp", "serve"], "env": {"T3_DATA_DIR": "/data"}}},
            "web_search": "disabled",
            "features": {"multi_agent": False},
        },
    }


def test_collaboration_items_are_counted_as_tools_and_translated_without_prompt_content(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="collab_success")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> list[object]:
        await session.start()
        try:
            await session.query("do the work")
            return [message async for message in session.receive_response()]
        finally:
            await session.close()

    messages = asyncio.run(run())
    tools = [
        block
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]

    assert [(tool.id, tool.name) for tool in tools] == [("collab-1", "Agent"), ("activity-1", "AgentActivity")]
    assert tools[0].input == {
        "tool": "spawnAgent",
        "model": "gpt-5.6-sol",
        "reasoningEffort": "high",
        "receiverThreadIds": ["child-1"],
    }
    assert "prompt" not in tools[0].input


def test_backend_default_model_is_omitted_from_thread_and_turn_requests(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, model=None)
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        await session.start()
        try:
            await session.query("use the Codex default")
            _ = [message async for message in session.receive_response()]
        finally:
            await session.close()

    asyncio.run(run())
    requests = _requests(log)

    for method in ("thread/start", "turn/start"):
        request = next(item for item in requests if item["method"] == method)
        assert "model" not in request["params"]


@pytest.mark.parametrize("scenario", ["initialize_error", "protocol_error", "unexpected_server_request", "api_key"])
def test_startup_failure_is_safe_and_always_reaps_the_child(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path, scenario: str
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario=scenario)
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> str:
        with pytest.raises((CodexAppServerError, HarnessFallbackError)) as raised:
            await session.start()
        assert session.process is not None
        assert session.process.returncode is not None
        return str(raised.value)

    message = asyncio.run(run())

    assert "do-not-print" not in message
    if scenario == "api_key":
        assert CODEX_APP_SERVER_CAPABILITIES.server_resume
        assert "teatree/codex/auth-json-b64" in message
        assert "t3 codex auth import" in message


@pytest.mark.parametrize(
    ("scenario", "kind", "side_effects_started"),
    [
        ("account_unauthorized", HarnessFallbackKind.AUTH, False),
        ("turn_error_quota", HarnessFallbackKind.QUOTA, False),
        ("turn_error_access", HarnessFallbackKind.ACCESS, False),
        ("turn_error_5xx", HarnessFallbackKind.PROVIDER_5XX, False),
        ("turn_error_quota_after_tool", HarnessFallbackKind.QUOTA, True),
        ("turn_error_quota_after_collab", HarnessFallbackKind.QUOTA, True),
    ],
)
def test_provider_failures_are_safe_typed_route_fallbacks(
    fake_codex: tuple[tuple[str, ...], Path],
    tmp_path: Path,
    scenario: str,
    kind: HarnessFallbackKind,
    side_effects_started,
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario=scenario)
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        if scenario == "account_unauthorized":
            await session.start()
            return
        await session.start()
        try:
            await session.query("do the work")
            _ = [message async for message in session.receive_response()]
        finally:
            await session.close()

    with pytest.raises(HarnessFallbackError) as raised:
        asyncio.run(run())

    assert raised.value.kind is kind
    assert raised.value.side_effects_started is side_effects_started
    assert raised.value.agent_session_id == (_THREAD_ID if scenario != "account_unauthorized" else "")
    assert "do-not-print" not in str(raised.value)


def test_programmer_or_schema_failure_is_not_route_fallback(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="turn_error_bad_request")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        await session.start()
        try:
            await session.query("do the work")
            _ = [message async for message in session.receive_response()]
        finally:
            await session.close()

    with pytest.raises(CodexAppServerError, match="turn/completed") as raised:
        asyncio.run(run())

    assert not isinstance(raised.value, HarnessFallbackError)
    assert "do-not-print" not in str(raised.value)


def test_stream_disconnect_after_accepted_turn_is_ambiguous_and_resumable(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="stream_disconnect_after_turn")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        await session.start()
        try:
            await session.query("do the work")
            _ = [message async for message in session.receive_response()]
        finally:
            await session.close()

    with pytest.raises(HarnessFallbackError) as raised:
        asyncio.run(run())

    assert raised.value.kind is HarnessFallbackKind.TRANSPORT
    assert raised.value.side_effects_started is True
    assert raised.value.agent_session_id == _THREAD_ID


def test_new_turn_request_failure_does_not_inherit_an_old_turns_side_effect_state(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="second_turn_start_quota")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        await session.start()
        try:
            await session.query("first turn")
            _ = [message async for message in session.receive_response()]
            await session.query("second turn")
        finally:
            await session.close()

    with pytest.raises(HarnessFallbackError) as raised:
        asyncio.run(run())

    assert raised.value.kind is HarnessFallbackKind.QUOTA
    assert raised.value.side_effects_started is False
    assert raised.value.agent_session_id == _THREAD_ID


def test_stderr_is_drained_while_requests_are_in_flight(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="stderr_flood")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> None:
        async with asyncio.timeout(3):
            await session.start()
            await session.close()

    asyncio.run(run())
    assert session._stream_failure is None


def test_cancelled_startup_always_reaps_the_child(fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="initialize_hang")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)
    initialize_sent = asyncio.Event()
    original_write = session._write

    async def observed_write(message: dict[str, object]) -> None:
        await original_write(message)
        if message.get("method") == "initialize":
            initialize_sent.set()

    async def run() -> None:
        with patch.object(session, "_write", side_effect=observed_write):
            start = asyncio.create_task(session.start())
            await initialize_sent.wait()
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
            assert session.process is not None
            assert session.process.returncode is not None

    asyncio.run(run())


def test_interrupt_is_correlated_while_receive_waits_for_notifications(
    fake_codex: tuple[tuple[str, ...], Path], tmp_path: Path
) -> None:
    command, log = fake_codex
    options, resume = _options(log, scenario="interrupt")
    session = _session(options, resume=resume, code_home=tmp_path / "home", command=command)

    async def run() -> ResultMessage:
        await session.start()
        try:
            await session.query("wait")
            receiver = asyncio.create_task(anext(session.receive_response()))
            await session.interrupt()
            message = await asyncio.wait_for(receiver, timeout=2)
            assert isinstance(message, ResultMessage)
            return message
        finally:
            await session.close()

    result = asyncio.run(run())

    interrupt = next(request for request in _requests(log) if request["method"] == "turn/interrupt")
    assert interrupt["params"] == {"threadId": _THREAD_ID, "turnId": "turn-1"}
    assert result.is_error is True
