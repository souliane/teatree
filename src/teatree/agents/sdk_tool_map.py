"""Map teatree's capability names to claude_sdk tool names — the Lane-A boundary (PR-11).

:mod:`teatree.core.modelkit.phase_tools` is the single source of truth for WHICH
tools a phase may call, named in teatree's own provider-neutral capability
vocabulary (the Lane-B ``FunctionToolset`` names). Lane A (``claude_sdk`` /
headless dispatch) speaks the bundled ``claude`` CLI's tool names, so this module
is the boundary translation the SSOT docstring points at: it maps each disallowed
capability to its SDK-native equivalent so ``_build_options`` can inject the
per-phase complement as ``ClaudeAgentOptions.disallowed_tools``.

A teatree-native capability with no SDK equivalent (``recall_memory``,
``record_attempt``) maps to the empty set — there is no CLI tool to deny. Every
SDK name here is a member of the bundled CLI's built-in registry (a name the CLI
does not know is rejected as a deny rule), pinned by the parity test.
"""

from typing import Final

from teatree.core.modelkit.phase_tools import disallowed_tools_for_phase

#: teatree capability name -> the ``claude_sdk`` built-in tool names that grant it.
#: ``MultiEdit`` is deliberately absent from ``edit_file`` — it was removed from
#: the current bundled CLI's tool registry, so naming it in a deny rule prints a
#: "matches no known tool" warning. The full capability vocabulary must be a key
#: here (drift guard: :func:`sdk_disallowed_tools_for_phase` would silently drop
#: an unmapped capability).
CAPABILITY_TO_SDK_TOOLS: Final[dict[str, frozenset[str]]] = {
    "read_file": frozenset({"Read"}),
    "write_file": frozenset({"Write"}),
    "edit_file": frozenset({"Edit", "NotebookEdit"}),
    "search_files": frozenset({"Grep", "Glob"}),
    "shell": frozenset({"Bash", "BashOutput", "KillBash", "KillShell"}),
    "web_fetch": frozenset({"WebFetch"}),
    "web_search": frozenset({"WebSearch"}),
    "dispatch_subtask": frozenset({"Agent", "Task"}),
    "recall_memory": frozenset(),
    "record_attempt": frozenset(),
}


def sdk_disallowed_tools_for_phase(phase: str) -> tuple[str, ...]:
    """Return the ``claude_sdk`` tool names *phase* may NOT call, sorted & deterministic.

    Maps :func:`~teatree.core.modelkit.phase_tools.disallowed_tools_for_phase`
    (the teatree-capability complement) to the SDK-native names Lane A injects. A
    write phase (coding / testing / e2e) whose complement is empty returns ``()``,
    so its dispatch options stay byte-identical to before this least-privilege
    lever existed.
    """
    names: set[str] = set()
    for capability in disallowed_tools_for_phase(phase):
        names |= CAPABILITY_TO_SDK_TOOLS.get(capability, frozenset())
    return tuple(sorted(names))
