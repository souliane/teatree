"""Permission gates for Lane B — the same refusals Lane A's PreToolUse enforces.

Two gate kinds, mirroring Lane A.

Hard-deny — a command that must never run (a main-clone working-tree mutation, a
privacy/banned-term leak). :func:`hard_deny_reason` is the single shared
evaluator; it consults the SAME importable ``teatree`` functions Lane A's
PreToolUse hook wraps (:func:`teatree.core.gates.main_clone_guard.find_main_clone_git_mutation`,
and :func:`teatree.hooks.quote_scanner.extract_publish_payload` → :func:`~teatree.hooks.quote_scanner.scan_text`
for the privacy scan — scoped to a PUBLISH payload, never every string argument),
so the two lanes refuse the identical set by construction, not by a parallel
re-implementation.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

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


def _publish_payload(command: str, cwd: Path | None) -> str | None:
    """The publish-egress text to scan, or ``None`` when the call is not a publish.

    Lane A's ``extract_publish_payload`` scoping, ported.
    Lane B's only egress surface is the ``shell`` command (its MCP toolsets are
    read-only and it dispatches no ``Agent``/``Task``), so a shell call is scoped
    exactly as Lane A scopes a ``Bash`` call: the body payload of a publish command
    (``gh``/``glab`` post, commit, …), ``None`` for a non-publish command. Every
    other tool — ``read_file``/``write_file``/``edit_file``/``search_files``, jailed
    to the worktree — has an empty *command* and is not scanned, matching Lane A,
    whose publish gate never scans a local file write.
    """
    if not command:
        return None
    from teatree.hooks.quote_scanner import extract_publish_payload  # noqa: PLC0415 (lazy, mirrors hard_deny_reason)

    return extract_publish_payload("Bash", {"command": command}, cwd)


def hard_deny_reason(tool_name: str, tool_args: dict[str, Any], *, cwd: Path | None = None) -> str | None:
    """Return the refusal reason for a tool call, or ``None`` when it is allowed.

    The single shared hard-deny evaluator, consulting the same ``teatree``
    functions Lane A's PreToolUse hook does. First, a command-tool call is run
    through the FULL main-clone gate (:func:`main_clone_git_deny_reason`),
    mirroring Lane A: it resolves the command's effective dir (honouring
    ``-C``/``--git-dir``) and refuses a ``checkout <feature>`` / ``reset --hard``
    / ``restore`` / ``stash pop`` ONLY when it targets a managed MAIN CLONE —
    the same routine worktree git ops Lane A ALLOWS pass here too, since Lane B
    tools are jailed to *cwd* (the worktree). Then the call's PUBLISH payload — the
    body of a publish command, resolved through the SAME
    :func:`~teatree.hooks.quote_scanner.extract_publish_payload` scoping Lane A's
    PreToolUse uses — is privacy-scanned; a HIGH finding refuses it. A non-publish
    call (a local ``write_file``, a non-egress shell command) yields no payload and
    is not scanned, so the two lanes refuse the identical publish set, not a wider
    everything-scan.
    """
    from teatree.core.gates.main_clone_env import main_clone_git_deny_reason  # noqa: PLC0415
    from teatree.hooks.quote_scanner import HIGH, scan_text  # noqa: PLC0415

    command = _command_of(tool_name, tool_args)
    if command:
        main_clone_reason = main_clone_git_deny_reason(command, cwd)
        if main_clone_reason is not None:
            return main_clone_reason

    payload = _publish_payload(command, cwd)
    if payload is not None:
        scan = scan_text(payload)
        high = next((f for f in scan.findings if f.severity == HIGH), None)
        if high is not None:
            return f"BLOCKED: privacy/banned-term gate — {high.name}: {high.excerpt!r}"

    return None


@dataclass
class HardDenyToolset(WrapperToolset[None]):
    """Wraps a toolset so every tool call is hard-deny-checked before it runs.

    A refused call raises :class:`ModelRetry` carrying the reason — pydantic_ai
    records it as a ``RetryPromptPart`` the model sees and must adapt to, the
    Lane-B analogue of a Lane-A PreToolUse deny message. The wrapped tool never
    executes. *cwd* is the worktree the Lane-B tools are jailed to; it keys the
    main-clone gate so the deny fires only when a command targets a managed main
    clone (a ``-C`` redirection out of the worktree), not for routine worktree
    git ops. It is a dataclass FIELD (not a plain attribute) because
    ``WrapperToolset`` rebuilds itself via ``dataclasses.replace`` in ``for_run``
    — a non-field would be dropped and reset each run.
    """

    cwd: Path | None = None

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> Any:  # noqa: ANN401 — the ``WrapperToolset.call_tool`` contract is ``-> Any``; matched here.
        reason = hard_deny_reason(name, tool_args, cwd=self.cwd)
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
