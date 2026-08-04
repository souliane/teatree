"""SessionStart(resume): admission for the fleet the harness restores in one step (#4108).

Background agents survive a harness restart — the harness restores them from their saved
transcripts and resumes them, all at once. Whatever stagger the orchestrator applied when it
dispatched them was a property of the DISPATCH, so it is not replayed: a fleet ramped up over
many minutes returns instantaneously (measured: an 8-core box went from healthy to load 58 with
1 GB free, with no intervening dispatch decision by anyone). A restore is not a dispatch, so the
dispatch-side gate (#4107) cannot see it either; this is that path's own admission check.

Two halves. ``handle_subagent_stop_track_agent`` records each terminating sub-agent, so the
restored set can be read as "dispatched MINUS terminated" rather than as the append-only
``<session>.agents`` ledger, which outlives the agents and would warn on history.
``resume_admission_advisory`` compares that count against the LIVE machine ceiling
(:func:`~teatree.core.admission_governor.resume_agent_ceiling`) and returns the shed directive
the router merges into its one SessionStart write.

Fail-open throughout, and ordered so an idle resume is free: the ledgers (two small file reads)
are consulted first, and the kill-switch — the only sqlite touch on the path — only once the
count is already over the ceiling. Any unreadable ledger, absent setting store, or import
failure yields no advisory: a detection bug must never break SessionStart.

Both ``teatree`` imports go through the shared ``managed_repo.teatree_src_on_path`` bootstrap:
the hook runs in the user's session shell with no guarantee ``teatree`` is importable (#1314),
and without it the fail-open path would swallow every ImportError and the gate would be silently
dead in exactly the environment it ships into.
"""

import contextlib
import sys

from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path
from hooks.scripts.state_files import append_line, read_lines

# Alias the bare and ``hooks.scripts.`` identities so the handler the router registers and a
# test patching a helper here operate on ONE module object (the ``subagent_no_commit`` contract).
sys.modules.setdefault("resume_admission", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.resume_admission", sys.modules[__name__])

_DISPATCHED_SUFFIX = "agents"
_STOPPED_SUFFIX = "agents-stopped"


def _ledger_ids(session_id: str, suffix: str) -> set[str]:
    """The agent ids in one per-session ledger.

    Both files are keyed by agent id in the first tab-delimited field — ``<session>.agents``
    carries a trailing role, ``<session>.agents-stopped`` carries the bare id — so one reader
    serves both and neither side has to strip anything to make the two match.
    """
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

    return {line.split("\t", 1)[0].strip() for line in read_lines(_state_file(session_id, suffix))}


def live_restored_agents(session_id: str) -> int:
    """How many dispatched sub-agents had NOT terminated — the set a resume brings back.

    An id in the stopped ledger that no dispatch recorded subtracts nothing (set difference,
    not arithmetic), so a stop the dispatch ledger never saw can never drive the count negative.
    """
    if not session_id:
        return 0
    return len(_ledger_ids(session_id, _DISPATCHED_SUFFIX) - _ledger_ids(session_id, _STOPPED_SUFFIX))


def handle_subagent_stop_track_agent(data: dict) -> None:
    """SubagentStop: record the terminating sub-agent so it stops counting as restored.

    Deduped on agent id, so a re-fired hook records it once. A payload with no ``agent_id``
    (the main agent's own Stop) records nothing. Best-effort — a record failure must never
    propagate out of the Stop hook.
    """
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    session_id = data.get("session_id", "")
    agent_id = str(data.get("agent_id") or "").strip()
    if not session_id or not agent_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        if agent_id not in _ledger_ids(session_id, _STOPPED_SUFFIX):
            append_line(_state_file(session_id, _STOPPED_SUFFIX), agent_id)


def _shed_directive(restored: int) -> str:
    """The governor's verdict on *restored* against the LIVE machine reading."""
    with _teatree_src_on_path():
        from teatree.core.admission_governor import (  # noqa: PLC0415 deferred: cold-hook import
            read_machine_signal,
            resume_shed_directive,
        )

        return resume_shed_directive(restored=restored, machine=read_machine_signal())


def _governor_enabled() -> bool:
    """The ``admission_governor_enabled`` kill-switch, read Django-free.

    The cold reader sees the ``ConfigSetting`` store, which is where the kill-switch lives —
    it is an explicit operator row by design, never an accidental default. Every cold read
    fails open to the default, so an absent or locked store leaves the advisory armed.
    """
    with _teatree_src_on_path():
        from teatree.config import cold_reader  # noqa: PLC0415 deferred: cold-hook import

        return cold_reader.bool_setting("admission_governor_enabled", default=True)


def resume_admission_advisory(session_id: str, source: str) -> str:
    """The shed directive for an over-ceiling resume, or ``""``.

    Silent for every other ``SessionStart`` source. A compaction in particular restores
    nothing — it is the same process with a compressed context — so gating it would fire on a
    fleet that never went anywhere.
    """
    if source != "resume":
        return ""
    try:
        restored = live_restored_agents(session_id)
        if restored == 0:
            return ""
        directive = _shed_directive(restored)
    except Exception:  # noqa: BLE001 — a detection bug must never break SessionStart
        return ""
    if not directive:
        return ""
    try:
        return directive if _governor_enabled() else ""
    except Exception:  # noqa: BLE001 — same fail-open contract; an unreadable switch stays armed
        return directive


__all__ = [
    "handle_subagent_stop_track_agent",
    "live_restored_agents",
    "resume_admission_advisory",
]
