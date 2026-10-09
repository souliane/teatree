"""Neutral option translation for the Codex App Server harness."""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.codex_sandbox import CONTAINER_IS_SANDBOX_ENV, container_is_the_sandbox
from teatree.agents.harness_options import HarnessOptions
from teatree.utils.ports import running_in_container

logger = logging.getLogger(__name__)

_MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
_SHELL_TOOLS = frozenset({"Bash", "BashOutput", "KillBash", "KillShell"})
_DISPATCH_TOOLS = frozenset({"Agent", "Task"})
_UNENFORCEABLE_DENIALS = _READ_TOOLS | _SHELL_TOOLS
_CLAUDE_ONLY_TOOLS = frozenset(
    {"AskUserQuestion", "Monitor", "PushNotification", "RemoteTrigger", "SendMessage", "WebFetch", "WebSearch"}
)
_CONTAINER_READ_ONLY_UNENFORCEABLE = "read-only inside a container: its sandbox cannot create a user namespace there"
_TEATREE_CODEX_PLUGIN_ID = "t3@souliane"
_SUPPORTED_PERMISSION_MODES = frozenset(
    {None, "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)


class CodexAppServerError(RuntimeError):
    @classmethod
    def missing_binary(cls) -> "CodexAppServerError":
        return cls("Codex App Server requires `codex` on PATH.")

    @classmethod
    def missing_thread_id(cls) -> "CodexAppServerError":
        return cls("Codex App Server did not return a thread id.")

    @classmethod
    def missing_turn(cls) -> "CodexAppServerError":
        return cls("Codex App Server completed without a turn result.")

    @classmethod
    def not_running(cls) -> "CodexAppServerError":
        return cls("Codex App Server is not running.")

    @classmethod
    def stopped(cls) -> "CodexAppServerError":
        return cls("Codex App Server stopped before completing the request.")

    @classmethod
    def invalid_protocol(cls) -> "CodexAppServerError":
        return cls("Codex App Server emitted an invalid JSONL protocol message.")

    @classmethod
    def oversize_line(cls, limit: int) -> "CodexAppServerError":
        return cls(f"Codex App Server emitted a protocol line over the {limit}-byte read limit.")

    @classmethod
    def refused_request(cls, method: str) -> "CodexAppServerError":
        return CodexAppServerRefusalError(f"Codex App Server refused {method!r}.")

    @classmethod
    def unsupported_policy(cls, detail: str) -> "CodexAppServerError":
        return cls(f"Codex App Server cannot enforce this unsupported tool policy: {detail}.")


class CodexAppServerRefusalError(CodexAppServerError):
    """The app server answered a request with a JSON-RPC error, or ended a turn with a terminal error."""


@dataclass(frozen=True, slots=True)
class CodexAppServerOptions:
    core: HarnessOptions
    runtime_workspace_roots: tuple[str, ...]
    sandbox_mode: str
    sandbox_policy: dict[str, Any]
    config: dict[str, Any]

    @classmethod
    def from_sdk_options(cls, options: ClaudeAgentOptions) -> "CodexAppServerOptions":
        if options.output_format is not None:
            _raise_unsupported("native structured output is not enabled")
        core = HarnessOptions.from_sdk_options(options)
        runtime_workspace_roots = _workspace_roots(core)
        sandbox_mode, sandbox_policy = _tool_policy(options, runtime_workspace_roots)
        config = _codex_config(options)
        if sandbox_mode == "danger-full-access":
            # A trusted project's `.codex/rules` ALLOW would skip the approval gate.
            config["projects"] = {root: {"trust_level": "untrusted"} for root in runtime_workspace_roots}
            # MCP tool calls are not approval requests, so the gate never sees them.
            config["plugins"] = {_TEATREE_CODEX_PLUGIN_ID: {"enabled": False}}
        return cls(
            core=core,
            runtime_workspace_roots=runtime_workspace_roots,
            sandbox_mode=sandbox_mode,
            sandbox_policy=sandbox_policy,
            config=config,
        )


def _raise_unsupported(detail: str) -> Never:
    raise CodexAppServerError.unsupported_policy(detail)


def codex_phase_policy_unavailable_reason(disallowed_tools: Iterable[str]) -> str | None:
    """Explain a Claude deny policy Codex 0.155.1 cannot faithfully enforce."""
    denied = set(disallowed_tools)
    if unsupported := sorted(denied & _UNENFORCEABLE_DENIALS):
        return "Codex cannot enforce tool denials for: " + ", ".join(unsupported)
    if denied & _MUTATION_TOOLS and container_is_the_sandbox():
        return f"Codex cannot enforce {_CONTAINER_READ_ONLY_UNENFORCEABLE}"
    return None


def codex_login_unavailable_reason(code_home: Path) -> str | None:
    """Stat the private home's ``auth.json`` without opening it; ``t3 codex auth check`` is what creates it."""
    if (code_home / "auth.json").is_file():
        return None
    return "no Codex login in the private home: run `t3 codex auth import`, then `t3 codex auth check`"


def codex_model_unavailable_reason(code_home: Path, model: str) -> str | None:
    """Fail open on a catalog this Codex release no longer writes in the shape read here."""
    if not model:
        return None
    catalog_path = code_home / "models_cache.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        slugs = {entry["slug"] for entry in catalog["models"]}
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Codex model catalog %s is unreadable; not checking %r against it", catalog_path, model)
        return None
    if model in slugs:
        return None
    return f"model {model!r} is not in the Codex catalog ({catalog_path}, fetched {catalog.get('fetched_at', '?')})"


def codex_container_unavailable_reason() -> str | None:
    """Explain why Codex cannot run in a container that has not opted in to being its sandbox."""
    if container_is_the_sandbox() or not running_in_container():
        return None
    return f"Codex's sandbox cannot start in a container; {CONTAINER_IS_SANDBOX_ENV}=1 makes the container the sandbox"


def _tool_policy(options: ClaudeAgentOptions, runtime_workspace_roots: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    if options.permission_mode not in _SUPPORTED_PERMISSION_MODES:
        _raise_unsupported(f"permission mode {options.permission_mode!r}")
    if options.allowed_tools:
        _raise_unsupported("Claude allowed-tools lists")
    if options.can_use_tool is not None or options.permission_prompt_tool_name is not None:
        _raise_unsupported("interactive permission callbacks")
    denied = set(options.disallowed_tools)
    if reason := codex_phase_policy_unavailable_reason(denied):
        _raise_unsupported(reason)
    if any(name.startswith("mcp__") for name in denied):
        _raise_unsupported("per-tool denials that Codex cannot represent")
    known = _MUTATION_TOOLS | _READ_TOOLS | _SHELL_TOOLS | _DISPATCH_TOOLS | _CLAUDE_ONLY_TOOLS
    if denied - known:
        _raise_unsupported("unknown per-tool denials")
    if options.permission_mode == "plan" or denied & _MUTATION_TOOLS:
        if container_is_the_sandbox():
            _raise_unsupported(_CONTAINER_READ_ONLY_UNENFORCEABLE)
        return "read-only", {"type": "readOnly"}
    if container_is_the_sandbox():
        # The container is the sandbox, as it already is for the claude_sdk lane beside it.
        return "danger-full-access", {"type": "dangerFullAccess"}
    # Claude's bypass mode means "do not prompt", not "grant every filesystem path".
    # Codex can preserve the unattended behavior with approvalPolicy=never while retaining
    # the task's explicit cwd/add_dirs boundary. Full host access would silently widen it.
    return "workspace-write", {"type": "workspaceWrite", "writableRoots": list(runtime_workspace_roots)}


def _workspace_roots(options: HarnessOptions) -> tuple[str, ...]:
    roots = ((options.cwd,) if options.cwd else ()) + options.add_dirs
    return tuple(dict.fromkeys(roots))


def _codex_config(options: ClaudeAgentOptions) -> dict[str, Any]:
    # Apps load the codex_apps MCP server, whose tool calls never reach the approval gate.
    features: dict[str, bool] = {"apps": False}
    config: dict[str, Any] = {"features": features}
    mcp_servers = _translate_mcp_servers(options.mcp_servers)
    if mcp_servers:
        config["mcp_servers"] = mcp_servers
    if "WebSearch" in options.disallowed_tools:
        config["web_search"] = "disabled"
    if _DISPATCH_TOOLS & set(options.disallowed_tools):
        features["multi_agent"] = False
    return config


def _translate_mcp_servers(value: object) -> dict[str, dict[str, Any]]:
    if value in ({}, None):
        return {}
    if not isinstance(value, dict):
        _raise_unsupported("non-inline MCP configuration")
    translated: dict[str, dict[str, Any]] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not isinstance(raw, dict) or raw.get("type", "stdio") != "stdio":
            _raise_unsupported("only stdio MCP servers are supported")
        command = raw.get("command")
        args = raw.get("args", [])
        env = raw.get("env", {})
        if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            _raise_unsupported("invalid stdio MCP command")
        valid_env = isinstance(env, dict) and all(
            isinstance(key, str) and isinstance(item, str) for key, item in env.items()
        )
        if not valid_env:
            _raise_unsupported("invalid stdio MCP environment")
        translated[name] = {"command": command, "args": args}
        if env:
            translated[name]["env"] = env
    return translated
