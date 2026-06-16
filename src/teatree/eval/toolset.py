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
"""

from teatree.eval.models import AnyOf, EvalSpec, Matcher, canonicalize_tool

#: The COMPLETE set of the bundled ``claude`` CLI's built-in tool names (24,
#: from ``strings`` on the binary). Because the set is complete, no built-in
#: (PushNotification etc.) can leak past the denylist.
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
    "MultiEdit",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "ReadMcpResource",
    "Task",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
)


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
    """
    return tuple(sorted(set(KNOWN_BUILTIN_TOOLS) - _available_tool_set(spec)))


__all__ = [
    "KNOWN_BUILTIN_TOOLS",
    "compute_available_tools",
    "compute_disallowed_tools",
]
