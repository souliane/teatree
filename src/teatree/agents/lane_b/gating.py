"""Permission gates for Lane B — the same refusals Lane A's PreToolUse enforces.

Two gate kinds, mirroring Lane A.

Hard-deny — a command that must never run (a main-clone working-tree mutation, a
privacy/banned-term leak). :func:`hard_deny_reason` is the single shared
evaluator; it consults the SAME importable ``teatree`` functions Lane A's
PreToolUse hook wraps (:func:`teatree.core.gates.main_clone_guard.find_main_clone_git_mutation`,
:func:`teatree.hooks.quote_scanner.scan_text`), so the two lanes refuse the
identical set by construction, not by a parallel re-implementation.
:class:`HardDenyToolset` wraps a toolset and raises the refusal into the model as
a ``RetryPromptPart`` (the model sees the reason and must adapt) exactly as a
Lane-A PreToolUse deny surfaces its message.

Soft-gate — a command that needs a human's approval (the ask-gate).
:func:`make_soft_gate_predicate` builds the ``approval_required`` predicate; a
matched tool call raises ``ApprovalRequired`` and the run's output becomes a
``DeferredToolRequests``, which the harness parks (the park->Slack->resume
machinery). Resumed with a ``DeferredToolResults`` mapping ``tool_call_id`` to
approve/deny.
"""

from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

# The branches a `git checkout <branch>` may safely target without the command
# being a main-clone mutation — mirrors the protected set Lane A resolves per
# repo. Passed to the shared core classifier so both lanes agree on which
# checkout targets are allowed.
_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "development", "release"})

#: Tools whose primary argument is a shell command (the Bash-parity surface).
_COMMAND_TOOLS = frozenset({"shell"})

#: Soft-gated tool names: a call to one needs human approval before it runs. The
#: shell tool is the ask-gate surface — a command that clears hard-deny may still
#: warrant a human's yes/no (the park→Slack→resume ask-gate).
DEFAULT_SOFT_GATED: frozenset[str] = frozenset()


def _command_of(tool_name: str, tool_args: dict[str, Any]) -> str:
    """The shell-command string of a command-tool call, else ``""``."""
    if tool_name in _COMMAND_TOOLS:
        value = tool_args.get("command", "")
        return value if isinstance(value, str) else ""
    return ""


def _scannable_text(tool_args: dict[str, Any]) -> str:
    """Every string argument joined — the text a privacy/banned-term scan reads."""
    return "\n".join(str(v) for v in tool_args.values() if isinstance(v, str))


def hard_deny_reason(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """Return the refusal reason for a tool call, or ``None`` when it is allowed.

    The single shared hard-deny evaluator, consulting the same ``teatree``
    functions Lane A's PreToolUse hook does. First, a command-tool call is
    classified by the pure core main-clone classifier — a ``git checkout
    <feature>`` / ``reset --hard`` / ``restore`` / ``stash pop`` is refused,
    while ``git fetch`` / ``checkout <default>`` / worktree ops pass. Then every
    string argument is privacy-scanned; a HIGH finding (a leaked secret / banned
    term) is refused.
    """
    from teatree.core.gates.main_clone_guard import deny_reason, find_main_clone_git_mutation  # noqa: PLC0415
    from teatree.hooks.quote_scanner import HIGH, scan_text  # noqa: PLC0415

    command = _command_of(tool_name, tool_args)
    if command:
        finding = find_main_clone_git_mutation(command, default_branch=None, protected_branches=_PROTECTED_BRANCHES)
        if finding is not None:
            return deny_reason(finding)

    scan = scan_text(_scannable_text(tool_args))
    high = next((f for f in scan.findings if f.severity == HIGH), None)
    if high is not None:
        return f"BLOCKED: privacy/banned-term gate — {high.name}: {high.excerpt!r}"

    return None


class HardDenyToolset(WrapperToolset[None]):
    """Wraps a toolset so every tool call is hard-deny-checked before it runs.

    A refused call raises :class:`ModelRetry` carrying the reason — pydantic_ai
    records it as a ``RetryPromptPart`` the model sees and must adapt to, the
    Lane-B analogue of a Lane-A PreToolUse deny message. The wrapped tool never
    executes.
    """

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> Any:  # noqa: ANN401 — the ``WrapperToolset.call_tool`` contract is ``-> Any``; matched here.
        reason = hard_deny_reason(name, tool_args)
        if reason is not None:
            raise ModelRetry(reason)
        return await super().call_tool(name, tool_args, ctx, tool)


def make_soft_gate_predicate(
    soft_gated: frozenset[str] = DEFAULT_SOFT_GATED,
) -> Callable[[RunContext[None], ToolDefinition, dict[str, Any]], bool]:
    """Build the ``approval_required`` predicate for the given soft-gated names.

    A call to a soft-gated tool raises ``ApprovalRequired`` (pydantic_ai's native
    deferred-approval primitive), surfacing the run's output as a
    ``DeferredToolRequests`` the harness parks. An empty *soft_gated* set (the
    default) gates nothing — byte-identical to no ask-gate.
    """

    def predicate(
        ctx: RunContext[None],  # noqa: ARG001 — pydantic_ai's approval_required callback contract.
        tool_def: ToolDefinition,
        args: dict[str, Any],  # noqa: ARG001 — same contract; only the tool name gates.
    ) -> bool:
        return tool_def.name in soft_gated

    return predicate


def raise_if_soft_gated(tool_name: str, soft_gated: frozenset[str] = DEFAULT_SOFT_GATED) -> None:
    """Raise ``ApprovalRequired`` when *tool_name* is soft-gated (test seam)."""
    if tool_name in soft_gated:
        raise ApprovalRequired
