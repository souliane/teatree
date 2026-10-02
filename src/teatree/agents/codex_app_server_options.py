"""Neutral option translation for the Codex App Server harness."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Never

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.harness_options import HarnessOptions

_MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
_SHELL_TOOLS = frozenset({"Bash", "BashOutput", "KillBash", "KillShell"})
_DISPATCH_TOOLS = frozenset({"Agent", "Task"})
_UNENFORCEABLE_DENIALS = _READ_TOOLS | _SHELL_TOOLS
_CLAUDE_ONLY_TOOLS = frozenset(
    {"AskUserQuestion", "Monitor", "PushNotification", "RemoteTrigger", "SendMessage", "WebFetch", "WebSearch"}
)
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
    def refused_request(cls, method: str) -> "CodexAppServerError":
        return cls(f"Codex App Server refused {method!r}.")

    @classmethod
    def unsupported_policy(cls, detail: str) -> "CodexAppServerError":
        return cls(f"Codex App Server cannot enforce this unsupported tool policy: {detail}.")


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
        return cls(
            core=core,
            runtime_workspace_roots=runtime_workspace_roots,
            sandbox_mode=sandbox_mode,
            sandbox_policy=sandbox_policy,
            config=_codex_config(options),
        )


def _raise_unsupported(detail: str) -> Never:
    raise CodexAppServerError.unsupported_policy(detail)


def codex_phase_policy_unavailable_reason(disallowed_tools: Iterable[str]) -> str | None:
    """Explain a Claude deny policy Codex 0.155.1 cannot faithfully enforce."""
    unsupported = sorted(set(disallowed_tools) & _UNENFORCEABLE_DENIALS)
    if not unsupported:
        return None
    return "Codex cannot enforce tool denials for: " + ", ".join(unsupported)


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
        return "read-only", {"type": "readOnly"}
    # Claude's bypass mode means "do not prompt", not "grant every filesystem path".
    # Codex can preserve the unattended behavior with approvalPolicy=never while retaining
    # the task's explicit cwd/add_dirs boundary. Full host access would silently widen it.
    return "workspace-write", {"type": "workspaceWrite", "writableRoots": list(runtime_workspace_roots)}


def _workspace_roots(options: HarnessOptions) -> tuple[str, ...]:
    roots = ((options.cwd,) if options.cwd else ()) + options.add_dirs
    return tuple(dict.fromkeys(roots))


def _codex_config(options: ClaudeAgentOptions) -> dict[str, Any]:
    config: dict[str, Any] = {}
    mcp_servers = _translate_mcp_servers(options.mcp_servers)
    if mcp_servers:
        config["mcp_servers"] = mcp_servers
    if "WebSearch" in options.disallowed_tools:
        config["web_search"] = "disabled"
    if _DISPATCH_TOOLS & set(options.disallowed_tools):
        config["features"] = {"multi_agent": False}
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
