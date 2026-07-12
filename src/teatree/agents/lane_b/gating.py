"""Permission gates for Lane B — the same refusals Lane A's PreToolUse enforces.

Two gate kinds, mirroring Lane A.

Hard-deny — a command that must never run. :func:`hard_deny_reason` is the single
shared evaluator; it composes THREE importable-``teatree`` deny families, each the
SAME code Lane A's PreToolUse gates consult, so the two lanes refuse the same set.

Family 1, the main-clone working-tree mutation, is
:func:`~teatree.core.gates.main_clone_env.main_clone_git_deny_reason`, scoped by
*cwd* (the worktree jail), exactly as Lane A's main-clone gate. Family 2, the
Bash-shaped hard-denies, is the shared
:func:`teatree.hooks.hard_deny_registry.hard_deny_reason`, iterating the ONE
registry (raw forge-merge, ``--no-verify``/hooksPath, secret-file-print,
raw-review-post, self-reviewer-assign, raw-pid-kill) that the cold PreToolUse
guards delegate to. Before this registry, Lane B checked only families 1 and 3, so
a raw forge merge or a hook-silencing push was reachable under
``agent_harness=pydantic_ai`` with NO MergeClear or CI verification (the "Lane-B
bypass" class, souliane/teatree#2 — closed here). Family 3, the privacy/banned-term
leak, is the publish payload
(:func:`~teatree.hooks.quote_scanner.extract_publish_payload`, ``None`` for a
non-publish call), privacy-scanned, then a HIGH finding routed through Lane A's OWN
destination gate (:func:`~teatree.hooks.public_visibility.gate_skips_for_visibility`
+ :func:`~teatree.hooks.publish_surface.command_targets_private_only`), never an
unconditional deny-any-HIGH.

The two lanes refuse the SAME set because they run the SAME predicates — not
"identical by construction" of two parallel re-implementations (that claim was
false: it omitted family 2 entirely). The deny-corpus parity test
(``tests/teatree_agents/lane_b/test_parity.py``) feeds every Lane-A deny fixture
through :func:`hard_deny_reason` and asserts identical refusals, so a future
divergence fails CI.

:class:`HardDenyToolset` wraps a toolset and raises the refusal into the model as
a ``RetryPromptPart`` (the model sees the reason and must adapt) exactly as a
Lane-A PreToolUse deny surfaces its message — under a per-run retry cap so a
predicate false-positive aborts the run cleanly instead of looping the model.

Soft-gate — a command that needs a human's approval (the ask-gate).
:func:`make_soft_gate_predicate` builds the ``approval_required`` predicate; a
matched tool call raises ``ApprovalRequired`` and the run's output becomes a
``DeferredToolRequests``, which the harness parks (the park->Slack->resume
machinery). Resumed with a ``DeferredToolResults`` mapping ``tool_call_id`` to
approve/deny.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry, UnexpectedModelBehavior
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


def _publish_high_denies(command: str, cwd: Path | None) -> bool:
    """The destination half of Lane A's ``resolve_high_verdict``, REUSED not reimplemented.

    A HIGH publish finding is refused ONLY when the command's target is
    affirmatively PUBLIC. Lane A SKIPS a non-public / unresolvable / unknown
    target (:func:`~teatree.hooks.public_visibility.gate_skips_for_visibility`)
    and DOWNGRADES a provably-private one to a warn
    (:func:`~teatree.hooks.publish_surface.command_targets_private_only`); only
    what neither skips nor downgrades — a confirmed-public egress — denies. Lane B
    calls the identical predicates, so a HIGH finding refuses the SAME destination
    set as Lane A, never a wider deny-any-HIGH that would over-block a private or
    unresolvable target Lane A allows.
    """
    from teatree.hooks.public_visibility import gate_skips_for_visibility  # noqa: PLC0415 (lazy import)
    from teatree.hooks.publish_surface import command_targets_private_only  # noqa: PLC0415 (lazy import)

    if gate_skips_for_visibility(command, cwd):
        return False
    return not command_targets_private_only(command, cwd)


def hard_deny_reason(tool_name: str, tool_args: dict[str, Any], *, cwd: Path | None = None) -> str | None:
    """Return the refusal reason for a tool call, or ``None`` when it is allowed.

    The single shared hard-deny evaluator, consulting the same ``teatree``
    functions Lane A's PreToolUse hook does, in three families. FIRST, a
    command-tool call is run through the FULL main-clone gate
    (:func:`main_clone_git_deny_reason`), mirroring Lane A: it resolves the
    command's effective dir (honouring ``-C``/``--git-dir``) and refuses a
    ``checkout <feature>`` / ``reset --hard`` / ``restore`` / ``stash pop`` ONLY
    when it targets a managed MAIN CLONE — the same routine worktree git ops Lane A
    ALLOWS pass here too, since Lane B tools are jailed to *cwd* (the worktree).
    SECOND, the command runs through the shared Bash-shaped hard-deny registry
    (:func:`teatree.hooks.hard_deny_registry.hard_deny_reason`) — the ONE set the
    cold PreToolUse guards also iterate (raw forge-merge, ``--no-verify``/hooksPath,
    secret-file-print, raw-review-post, self-reviewer-assign, raw-pid-kill), so a
    raw ``gh pr merge`` / ``git push --no-verify`` is denied here exactly as Lane A
    denies it. THIRD, the call's PUBLISH payload — the body of a publish command,
    resolved through the SAME
    :func:`~teatree.hooks.quote_scanner.extract_publish_payload` scoping Lane A's
    PreToolUse uses — is privacy-scanned; a HIGH finding is routed through Lane A's
    OWN destination gate (:func:`_publish_high_denies`) and refuses the call ONLY
    when the target is a confirmed-PUBLIC egress — a non-public / unresolvable /
    provably-private target is allowed, exactly as Lane A's ``resolve_high_verdict``
    skips or downgrades it. A non-publish call (a local ``write_file``, a non-egress
    shell command) yields no payload and is not scanned, so the two lanes refuse the
    identical publish set, not a wider everything-scan nor a wider deny-any-HIGH.
    """
    from teatree.core.gates.main_clone_env import main_clone_git_deny_reason  # noqa: PLC0415 — lazy import
    from teatree.hooks.hard_deny_registry import (  # noqa: PLC0415 (lazy, mirrors the other in-function hard-deny imports)
        hard_deny_reason as bash_hard_deny_reason,
    )
    from teatree.hooks.quote_scanner import HIGH, scan_text  # noqa: PLC0415 — deferred: call-time import, kept lazy

    command = _command_of(tool_name, tool_args)
    if command:
        main_clone_reason = main_clone_git_deny_reason(command, cwd)
        if main_clone_reason is not None:
            return main_clone_reason
        registry_reason = bash_hard_deny_reason(command)
        if registry_reason is not None:
            return registry_reason

    payload = _publish_payload(command, cwd)
    if payload is not None:
        scan = scan_text(payload)
        high = next((f for f in scan.findings if f.severity == HIGH), None)
        if high is not None and _publish_high_denies(command, cwd):
            return f"BLOCKED: privacy/banned-term gate — {high.name}: {high.excerpt!r}"

    return None


#: How many hard-denies one run may raise before the run is aborted. A refusal is
#: a ``ModelRetry`` the model is meant to adapt to; but a predicate FALSE-positive
#: it cannot satisfy (or a genuinely-blocked path the model keeps re-attempting
#: with variations) would loop, burning tokens. Past this cap the deny becomes a
#: terminal :class:`UnexpectedModelBehavior` that ends the run instead of retrying.
_DEFAULT_MAX_DENIALS: int = 3

#: Cap on how many distinct runs' denial tallies :class:`HardDenyToolset` retains.
#: ``denial_counts`` is shared across every ``for_run`` copy for the toolset's whole
#: life, so without a bound it grows one entry per run forever. Only the in-flight
#: run's tally is ever read, so evicting the oldest-inserted keys once the cap is
#: crossed cannot lose a live count.
_MAX_TRACKED_RUNS: int = 256


def _bound_denial_counts(counts: dict[str, int]) -> None:
    """Evict oldest-inserted run tallies until *counts* fits :data:`_MAX_TRACKED_RUNS`."""
    while len(counts) > _MAX_TRACKED_RUNS:
        counts.pop(next(iter(counts)))


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

    *max_denials* caps how many hard-denies a single run may raise: past it the
    deny is a terminal :class:`UnexpectedModelBehavior` (the run ends) rather than
    a :class:`ModelRetry`, so a predicate false-positive cannot loop the model.
    *denial_counts* tallies denials per run; it is a shared-by-``replace`` field
    (``for_run`` rebuilds the toolset), so all per-run copies increment the same
    tally and each run's count is isolated by its own key. A run with no ``run_id``
    keys off the context's identity rather than a shared ``""`` bucket (which would
    pool unrelated runs' denials and trip the cap early), and the tally is bounded
    to :data:`_MAX_TRACKED_RUNS` so it cannot grow one entry per run forever.
    """

    cwd: Path | None = None
    max_denials: int = _DEFAULT_MAX_DENIALS
    denial_counts: dict[str, int] = field(default_factory=dict)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> Any:  # noqa: ANN401 — the ``WrapperToolset.call_tool`` contract is ``-> Any``; matched here.
        reason = hard_deny_reason(name, tool_args, cwd=self.cwd)
        if reason is not None:
            run_key = getattr(ctx, "run_id", "") or f"anon-{id(ctx)}"
            count = self.denial_counts.get(run_key, 0) + 1
            self.denial_counts[run_key] = count
            _bound_denial_counts(self.denial_counts)
            if count >= self.max_denials:
                cap_reached = (
                    f"Lane-B hard-deny retry cap reached ({self.max_denials} refusals this run); "
                    f"aborting rather than looping. Last refusal — {reason}"
                )
                raise UnexpectedModelBehavior(cap_reached)
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
