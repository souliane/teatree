"""Lane-B (``pydantic_ai``) tool-capability layer (PR-03, souliane/teatree#2512).

Closes the zero-tools gap: the ``pydantic_ai`` runtime lane was text-in/text-out
with no tools, MCP, permissions, or compaction. This package supplies each,
adopting pydantic_ai's OWN native primitives (``FunctionToolset`` /
``CombinedToolset`` / ``FilteredToolset`` / ``WrapperToolset`` / the
``approval_required`` deferral / the MCP client) rather than an external
harness package:

* :mod:`teatree.agents.lane_b.config` — the config shim (worktree jail, phase).
* :mod:`teatree.agents.lane_b.filesystem` — read/write/edit/search, jailed.
* :mod:`teatree.agents.lane_b.shell` — the denylist/timeout command runner.
* :mod:`teatree.agents.lane_b.gating` — hard-deny (shared registry) + soft-gate.
* :mod:`teatree.agents.lane_b.mcp` — teatree's read-only MCP toolset.
* :mod:`teatree.agents.lane_b.compaction` — the context-trim history processor.
* :mod:`teatree.agents.lane_b.toolsets` — the composed, phase-scoped assembly.
"""
