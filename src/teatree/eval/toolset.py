"""Derive a scenario's clean-room toolset (allowlist + denylist complement).

A metered run will SPIRAL into any built-in a scenario did not declare —
tool-hunting (``ToolSearch``), punting (``AskUserQuestion``), notifying
(``PushNotification``) — burning ``max_turns`` on exploration the matchers never
asked for (a false fail). The toolset is restricted by TWO belt-and-suspenders
mechanisms that always agree.

The PRIMARY ``--tools`` allowlist (``ClaudeAgentOptions.tools``) is
:func:`compute_available_tools`: the model SEES only the listed tools,
independent of ``permission_mode``. The DEFENSE-IN-DEPTH ``--disallowedTools``
complement is :func:`compute_disallowed_tools` = :data:`KNOWN_BUILTIN_TOOLS`
MINUS the available set — exhaustive even if a CLI build ignored ``--tools``.

Delegation scenarios additionally need the ``Agent`` SPAWN tool both ALLOWLISTED
and BACKED by a sub-agent definition: :func:`scenario_exposes_subagent_spawn`
decides when a scenario can reach a spawn tool, and :func:`build_delegation_agents`
provisions the generic delegate the runner hands to ``ClaudeAgentOptions.agents``.
"""

from claude_agent_sdk import AgentDefinition

from teatree.eval.models import AnyOf, EvalSpec, Matcher, canonicalize_tool

#: The COMPLETE set of the bundled ``claude`` CLI's built-in tool names (26,
#: validated against the binary — a name the CLI does NOT register is REJECTED
#: with ``Permission deny rule "<name>" matches no known tool — check for
#: typos.`` on EVERY ``--disallowedTools`` invocation. Because the set is
#: complete, no built-in (PushNotification etc.) can leak past the denylist.
#:
#: ``MultiEdit`` was REMOVED from the CLI's tool registry (current bundled CLI
#: 2.1.x). Leaving it here named a tool the CLI no longer knows, so EVERY
#: clean-room SDK invocation printed the "matches no known tool" warning —
#: harmless on its own (the rule is just dropped) but a stale, noisy denylist
#: that no longer agreed with the binary. The drift-detecting parity test
#: ``tests/teatree_eval/test_toolset_parity.py`` probes the bundled binary so a
#: future add/remove fails CI instead of drifting silently.
#:
#: The three Agent-Team tools — ``SendMessage``, ``TaskCreate``, ``TaskUpdate`` —
#: are genuine CLI built-ins the team-mode runtime grants (verified by ``strings``
#: on the binary: each carries its own tool description, e.g. "TaskCreate adds an
#: item to the task list and takes ``subject`` and ``description``…" and "use the
#: SendMessage tool with ``to: "<name>"`` to send messages to specific teammates").
#: They were MISSING from the set, so the team scenarios that declare them
#: (``team_mode_delegates_to_fixed_roster_not_spawn_per_task``,
#: ``team_mate_spawned_opus_never_sonnet``) produced a ``--tools`` allowlist whose
#: denylist-complement was NOT exhaustive over the real built-in set — an
#: incomplete set is exactly the leak this constant exists to prevent. Including
#: them keeps the allowlist/denylist pair consistent so the ``Agent`` tool the
#: spawn-vs-delegate scenarios depend on is reliably exposed.
#:
#: ``Skill`` is deliberately ABSENT — it is left untouched so a scenario can always
#: load a skill (the CLI auto-appends ``Skill`` to the allowlist anyway).
KNOWN_BUILTIN_TOOLS: tuple[str, ...] = (
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "EnterPlanMode",
    "ExitPlanMode",
    "Glob",
    "Grep",
    "KillBash",
    "KillShell",
    "ListMcpResources",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "ReadMcpResource",
    "SendMessage",
    "Task",
    "TaskCreate",
    "TaskUpdate",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
)

#: The canonical CLI sub-agent SPAWN tool name. The bundled ``claude`` registers
#: the delegate-to-a-sub-agent tool as ``Agent`` (NOT ``Task`` — ``Task`` resolves
#: to no known tool; see ``models._TOOL_ALIASES``). Co-located with the toolset
#: seam so the runner and the toolset agree on the one name that gates delegation.
SUBAGENT_SPAWN_TOOL = "Agent"

#: The name of the generic delegation subagent the runner provisions for any
#: scenario whose toolset exposes :data:`SUBAGENT_SPAWN_TOOL`. The model invokes it
#: via ``Agent(subagent_type="delegate", prompt=...)``.
DELEGATION_SUBAGENT_NAME = "delegate"


def _matcher_referenced_tools(spec: EvalSpec) -> set[str]:
    """The canonical tool names every MATCHER in *spec* references.

    Collects ``Matcher.tool`` (positive AND negative) and each
    ``AnyOf.alternatives`` entry's tool; ``FinalStateMatcher`` references no tool.
    A negative matcher's tool is included on PURPOSE: removing the tool a
    ``no_tool_call_matching`` assertion guards would make that assertion pass
    vacuously, hiding the misbehaviour it tests — so it must stay available.
    """
    referenced: set[str] = set()
    for matcher in spec.matchers:
        if isinstance(matcher, Matcher):
            referenced.add(canonicalize_tool(matcher.tool))
        elif isinstance(matcher, AnyOf):
            referenced.update(canonicalize_tool(alt.tool) for alt in matcher.alternatives)
    return referenced


def _available_tool_set(spec: EvalSpec) -> set[str]:
    """The canonical set of tools the model is ALLOWED to see for *spec*.

    The union of the scenario's declared ``spec.tools`` (canonicalized the SAME
    way the grader canonicalizes, so the lowercase ``bash`` alias matches ``Bash``)
    and every matcher-referenced tool. A matcher-referenced tool is always
    available so a negative assertion can still observe the misbehaviour it tests.
    The single source of truth for BOTH the allowlist and the denylist-complement.
    """
    return {canonicalize_tool(tool) for tool in spec.tools} | _matcher_referenced_tools(spec)


def compute_available_tools(spec: EvalSpec) -> tuple[str, ...]:
    """The ``--tools`` ALLOWLIST for *spec* — the model sees ONLY these tools.

    The PRIMARY toolset restriction: ``canonicalize(spec.tools)`` unioned with
    every matcher-referenced tool, sorted and deterministic. Independent of
    ``permission_mode``. Empty when a scenario declares no tools and references
    none — ``build_sdk_options`` renders that as ``tools=None`` (the CLI default
    toolset), never an empty ``--tools ""`` (no tools).
    """
    return tuple(sorted(_available_tool_set(spec)))


def compute_disallowed_tools(spec: EvalSpec) -> tuple[str, ...]:
    """The built-in tools to REMOVE from the model's toolset for *spec* (denylist).

    DEFENSE-IN-DEPTH complement of :func:`compute_available_tools` within the
    complete :data:`KNOWN_BUILTIN_TOOLS`: a built-in is disallowed unless it is in
    the available set. Because the set is complete, this is exhaustive even if a
    CLI build ignored ``--tools``. Sorted for a deterministic, idempotent set.

    EXCEPTION — a delegation scenario gets an EMPTY denylist. The bundled CLI
    disables the ``Agent`` SPAWN tool whenever ANY ``--disallowedTools`` denylist
    is present (verified against the binary: with ``Agent`` in the ``--tools``
    allowlist and NOT in the denylist, a non-empty denylist still strips ``Agent``
    from the model's toolset — the sub-agent capability is gated on the denylist
    being empty, not on ``Agent`` itself being denied). The ``--tools`` allowlist
    is the PRIMARY restriction and ALONE confines the toolset to the declared set
    (verified: ``tools=[Agent, Bash]`` + empty denylist shows the model exactly
    ``Agent`` + ``Bash`` — no spiral tools leak), so dropping the defense-in-depth
    denylist for delegation scenarios keeps the spawn tool usable WITHOUT widening
    the toolset. Non-delegation scenarios keep the full denylist unchanged.
    """
    if SUBAGENT_SPAWN_TOOL in _available_tool_set(spec):
        return ()
    return tuple(sorted(set(KNOWN_BUILTIN_TOOLS) - _available_tool_set(spec)))


def scenario_exposes_subagent_spawn(spec: EvalSpec) -> bool:
    """True when *spec*'s toolset exposes the CLI's ``Agent`` sub-agent spawn tool.

    A scenario opts into delegation by declaring ``Task`` (canonicalized to
    ``Agent``) or ``Agent`` in its ``tools`` — or by referencing it from a matcher.
    Both routes land :data:`SUBAGENT_SPAWN_TOOL` in :func:`compute_available_tools`,
    so the runner provisions an ``agents`` definition exactly when the model can
    actually reach a spawn tool (and never for a non-delegation scenario).
    """
    return SUBAGENT_SPAWN_TOOL in _available_tool_set(spec)


def build_delegation_agents(spec: EvalSpec) -> dict[str, AgentDefinition] | None:
    """A generic delegation sub-agent for *spec*, or ``None`` when it can't delegate.

    Returns a single ``{DELEGATION_SUBAGENT_NAME: AgentDefinition}`` when the
    scenario's toolset exposes the ``Agent`` spawn tool
    (:func:`scenario_exposes_subagent_spawn`) — so the model can actually delegate
    to a defined sub-agent rather than only the built-in ``general-purpose`` one —
    and ``None`` otherwise, leaving every non-delegation scenario's
    ``ClaudeAgentOptions.agents`` at its ``None`` default (no behaviour change).

    The sub-agent is deliberately GENERIC: the delegation scenarios assert that the
    main agent ISSUES a spawn call (``tool_call: Agent`` with a prompt describing
    the delegated unit) and does NOT do the work in the foreground — they do not
    grade the sub-agent's own trajectory. A broad description + the inherited model
    is enough to make the spawn legitimate. ``model="inherit"`` keeps the sub-agent
    on the scenario's own model so a delegation trial is not billed at a surprise tier.
    """
    if not scenario_exposes_subagent_spawn(spec):
        return None
    return {
        DELEGATION_SUBAGENT_NAME: AgentDefinition(
            description=(
                "General-purpose delegate. Use this sub-agent to carry out a bounded "
                "unit of delegated work off the main agent's foreground — a multi-file "
                "investigation, a refactor, writing a test suite, or a scoped code fix — "
                "and report the result back."
            ),
            prompt=(
                "You are a delegated worker sub-agent. Carry out the bounded unit of work "
                "described in your prompt — investigate, refactor, write tests, or apply a "
                "scoped fix as asked — then report your findings or the result back to the "
                "orchestrator. Stay within the unit you were handed."
            ),
            model="inherit",
        )
    }


__all__ = [
    "DELEGATION_SUBAGENT_NAME",
    "KNOWN_BUILTIN_TOOLS",
    "SUBAGENT_SPAWN_TOOL",
    "build_delegation_agents",
    "compute_available_tools",
    "compute_disallowed_tools",
    "scenario_exposes_subagent_spawn",
]
