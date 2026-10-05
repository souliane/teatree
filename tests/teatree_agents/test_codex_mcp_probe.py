"""The MCP probe judges one thread's server list and refuses whatever the approval gate cannot see."""

import asyncio
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_options
from teatree.agents.codex_app_server_options import (
    CodexAppServerError,
    CodexAppServerOptions,
    CodexAppServerRefusalError,
)
from teatree.agents.codex_mcp_probe import refuse_unjudged_mcp_servers
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind


@pytest.fixture
def unsandboxed(monkeypatch: pytest.MonkeyPatch) -> CodexAppServerOptions:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    return CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions"))


def _probe(
    response: dict[str, Any] | BaseException, options: CodexAppServerOptions
) -> list[tuple[str, dict[str, Any]]]:
    asked: list[tuple[str, dict[str, Any]]] = []

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        asked.append((method, params))
        if isinstance(response, BaseException):
            raise response
        return response

    asyncio.run(refuse_unjudged_mcp_servers(request, "thread-1", options))
    return asked


def test_a_thread_with_no_server_passes_after_being_asked_about_itself(unsandboxed: CodexAppServerOptions) -> None:
    asked = _probe({"data": [], "nextCursor": None}, unsandboxed)

    assert asked == [("mcpServerStatus/list", {"threadId": "thread-1", "detail": "toolsAndAuthOnly"})]


def test_a_sandboxed_thread_is_not_asked() -> None:
    sandboxed = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions")
    )

    assert _probe({"data": [{"name": "teatree"}]}, sandboxed) == []


def test_every_listed_server_is_named_in_the_access_refusal(unsandboxed: CodexAppServerOptions) -> None:
    with pytest.raises(HarnessFallbackError, match="codex_apps, teatree") as refused:
        _probe({"data": [{"name": "teatree"}, {"name": "codex_apps"}]}, unsandboxed)

    assert refused.value.kind is HarnessFallbackKind.ACCESS
    assert refused.value.side_effects_started is False
    assert refused.value.agent_session_id == "thread-1"


def test_a_next_page_with_no_data_is_still_a_refusal(unsandboxed: CodexAppServerOptions) -> None:
    with pytest.raises(HarnessFallbackError, match="unlisted"):
        _probe({"data": [], "nextCursor": "page-2"}, unsandboxed)


@pytest.mark.parametrize("response", [{}, {"data": 5}, {"data": None}])
def test_data_that_is_not_a_list_is_an_unreadable_status(unsandboxed: CodexAppServerOptions, response: dict) -> None:
    with pytest.raises(HarnessFallbackError, match="could not be read"):
        _probe(response, unsandboxed)


def test_a_json_rpc_refusal_is_an_unreadable_status(unsandboxed: CodexAppServerOptions) -> None:
    with pytest.raises(HarnessFallbackError, match="could not be read") as refused:
        _probe(CodexAppServerRefusalError("refused"), unsandboxed)

    assert isinstance(refused.value.__cause__, CodexAppServerRefusalError)


@pytest.mark.parametrize(
    "failure",
    [CodexAppServerError.stopped(), CodexAppServerError.not_running(), CodexAppServerError.invalid_protocol()],
    ids=["stopped", "not-running", "invalid-protocol"],
)
def test_a_failure_of_the_app_server_itself_surfaces_as_itself(
    unsandboxed: CodexAppServerOptions, failure: CodexAppServerError
) -> None:
    with pytest.raises(CodexAppServerError) as raised:
        _probe(failure, unsandboxed)

    assert raised.value is failure
    assert not isinstance(raised.value, HarnessFallbackError)


def test_a_typed_fallback_from_the_transport_is_left_alone(unsandboxed: CodexAppServerOptions) -> None:
    failure = HarnessFallbackError("dropped", kind=HarnessFallbackKind.TRANSPORT)

    with pytest.raises(HarnessFallbackError) as raised:
        _probe(failure, unsandboxed)

    assert raised.value is failure


def test_a_request_that_never_answers_is_refused_within_the_bound(
    monkeypatch: pytest.MonkeyPatch, unsandboxed: CodexAppServerOptions
) -> None:
    monkeypatch.setattr("teatree.agents.codex_mcp_probe.MCP_PROBE_SECONDS", 0.2)

    async def silent(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    with pytest.raises(HarnessFallbackError, match="did not answer in time"):
        asyncio.run(asyncio.wait_for(refuse_unjudged_mcp_servers(silent, "thread-1", unsandboxed), timeout=10))
