"""Thin config shim mapping teatree's gate config onto the Lane-B tool knobs.

Lane B (``pydantic_ai``, PR-03) adopts teatree-owned Shell and File System
capabilities. This module is the ONLY place that maps a dispatch's context (the
worktree cwd, the phase, the gate settings) onto the concrete knobs those
capabilities read — no capability module reaches into Django settings itself, so
the whole tool layer is driven by one injectable dataclass a test can build by
hand with no DB.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from teatree.agents.harness_options import HarnessOptions

#: Shell command prefixes refused outright on Lane B regardless of phase — the
#: irreversible/destructive set. This is a coarse denylist ON TOP OF the shared
#: hard-deny gate registry (:mod:`teatree.agents.lane_b.gating`); the registry is
#: the authoritative parity surface, this is a cheap first cut.
_DEFAULT_SHELL_DENYLIST: tuple[str, ...] = (
    "rm -rf /",
    "shutdown",
    "reboot",
    "mkfs",
    ":(){",  # fork bomb
)

#: A generous per-command wall-clock ceiling. A genuinely long build/test step is
#: bounded by the run-level watchdog, not this; the per-command timeout only trips
#: a single hung invocation.
_DEFAULT_SHELL_TIMEOUT_SECONDS: float = 600.0


@dataclass(frozen=True)
class LaneBToolConfig:
    """Everything the Lane-B tool layer needs, resolved once per dispatch.

    ``fs_root`` is the worktree the File System capability is jailed to; every
    read/write/edit/search path is resolved WITHIN it (path-traversal
    prevention). ``None`` when the task has no on-disk worktree, which disables
    the write/edit tools (a read against an absolute path outside a root is still
    refused). ``phase`` is the canonical phase token; it drives the phase-scoped
    toolset filter (:mod:`teatree.core.modelkit.phase_tools`). Empty string = no
    phase-scoping (every assembled tool is exposed), the construction-time
    default so an un-phased ``PydanticAiHarness()`` stays text-only.
    ``shell_denylist`` / ``shell_timeout_seconds`` are the coarse Shell knobs.
    ``shell_env`` is the RESOLVED child environment (base ``os.environ`` MERGED
    with any pinned overrides), never a bare override set — a subprocess ``env=``
    REPLACES the environment, so passing only the credential overrides would strip
    ``PATH``/``HOME`` from every shell (and then even ``bash`` would not resolve).
    An empty ``shell_env`` means "no overrides pinned → inherit the ambient env".
    """

    fs_root: Path | None = None
    phase: str = ""
    shell_denylist: tuple[str, ...] = _DEFAULT_SHELL_DENYLIST
    shell_timeout_seconds: float = _DEFAULT_SHELL_TIMEOUT_SECONDS
    shell_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_options(cls, options: HarnessOptions, *, phase: str = "") -> "LaneBToolConfig":
        """Build the tool config from the neutral harness *options* + *phase* (#3157 AH-2).

        Takes the provider-agnostic :class:`~teatree.agents.harness_options.HarnessOptions`, not
        the vendor ``ClaudeAgentOptions`` — the tool layer's knobs (cwd, env) are
        provider-agnostic, so the vendor type is confined to the harness ``open`` boundary.

        ``options.cwd`` is the worktree :func:`teatree.agents._headless_options._resolve_task_cwd`
        resolved for the task, so it is the natural File System jail root; a
        falsy cwd (no on-disk worktree) leaves ``fs_root`` ``None``.

        ``options.env`` (the pinned-credential child env, if any) is MERGED OVER a
        snapshot of ``os.environ`` — matching how the ``claude-agent-sdk`` child is
        spawned (the SDK merges ``options.env`` onto the inherited environment). A
        subprocess ``env=`` REPLACES the whole environment, so the merge is what
        keeps ``PATH``/``HOME`` present in the Lane-B shell; passing only the
        credential overrides would strip them from every command. When no override
        is pinned, ``shell_env`` stays empty so the Shell tool inherits the ambient
        env unchanged (``env=None``), byte-identical to before the credential port.
        """
        cwd = options.cwd
        fs_root = Path(cwd) if cwd else None
        overrides = dict(options.env or {})
        shell_env = {**os.environ, **overrides} if overrides else {}
        return cls(fs_root=fs_root, phase=phase, shell_env=shell_env)
