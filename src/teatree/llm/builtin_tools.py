"""The complete, binary-validated set of the bundled ``claude`` CLI's built-in tools.

The single source of truth for "every tool name the CLI knows", shared by two layers
that must agree on it: :mod:`teatree.eval.toolset` (the clean-room denylist complement)
and :mod:`teatree.agents._headless_options` (the #116 reader phase's exhaustive
tool-lockdown). It lives in the ``teatree.llm`` foundation layer so both the
``integration``-layer eval runner and the ``domain``-layer agents dispatch can import
it without a backward dependency edge.

The set is COMPLETE, so a denylist derived as ``KNOWN_BUILTIN_TOOLS - available`` is
exhaustive: no built-in (``PushNotification`` / ``RemoteTrigger`` / ``ToolSearch`` etc.)
can leak past it. The drift-detecting parity test
``tests/teatree_eval/test_toolset_parity.py`` probes the bundled binary so a future
add/remove fails CI instead of drifting silently.
"""

#: The COMPLETE set of the bundled ``claude`` CLI's built-in tool names (28,
#: validated against the binary — a name the CLI does NOT register is REJECTED
#: with ``Permission deny rule "<name>" matches no known tool — check for
#: typos.`` on EVERY ``--disallowedTools`` invocation. Because the set is
#: complete, no built-in (PushNotification etc.) can leak past the denylist.
#:
#: ``MultiEdit`` was REMOVED from the CLI's tool registry (current bundled CLI
#: 2.1.x). Leaving it here named a tool the CLI no longer knows, so EVERY
#: clean-room SDK invocation printed the "matches no known tool" warning —
#: harmless on its own (the rule is just dropped) but a stale, noisy denylist
#: that no longer agreed with the binary.
#:
#: The three Agent-Team tools — ``SendMessage``, ``TaskCreate``, ``TaskUpdate`` —
#: are genuine CLI built-ins the team-mode runtime grants (verified by ``strings``
#: on the binary: each carries its own tool description). Including them keeps the
#: allowlist/denylist pair consistent so the ``Agent`` tool the spawn-vs-delegate
#: scenarios depend on is reliably exposed.
#:
#: ``DesignSync`` and ``RemoteTrigger`` (#2601) are two further genuine CLI
#: built-ins of the same ``strings``-on-the-binary provenance, and the bundled CLI
#: ACCEPTS both as ``--disallowedTools`` deny rules (only a bogus name is rejected).
#:
#: ``Skill`` is deliberately ABSENT — it is left untouched so a scenario can always
#: load a skill (the CLI auto-appends ``Skill`` to the allowlist anyway).
KNOWN_BUILTIN_TOOLS: tuple[str, ...] = (
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "DesignSync",
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
    "RemoteTrigger",
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

__all__ = ["KNOWN_BUILTIN_TOOLS"]
