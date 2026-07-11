"""SubagentStop: record a sub-agent that terminated without committing (#1205).

An ``isolation: worktree`` sub-agent that only edits files and never commits
loses ALL its work when the worktree is auto-cleaned on teardown, yet the
orchestrator believes work landed — a phantom-completion source (3x recurrence).
This SubagentStop handler runs once per sub-agent termination and, when the
sub-agent's worktree (the harness ``cwd``) shows a WORK branch with ZERO commits
ahead of its base, records a ``terminated_without_commit`` signal so the
orchestrator can SEE the empty termination instead of assuming success.

It is a DETECTION/surfacing hook, not a deny — SubagentStop cannot un-terminate
the agent. The signal is recorded through the SAME durable seam the
dispatched-sub-agent roster uses: a per-session ``<session>.no-commit`` state
file (mirrors ``<session>.agents``), which the PreCompact recovery snapshot
already reads back and renders so it survives compaction. A structured stderr
line (this module's logging channel) carries the same fact for the live
transcript.

Crash-proof and conservative (the #810 Stop-hook contract): a detached/read-only
review worktree (detached HEAD or a base branch) and a sub-agent that DID commit
are NOT flagged, and ANY inability to introspect git fails OPEN (never flag) — a
detection bug must never manufacture a false alarm. The decision logic lives in
the pure ``teatree.hooks.no_commit_detector`` leaf, imported function-scoped
after the ``src/`` bootstrap; this module is the thin worktree-resolving +
signal-recording wrapper. A ``hooks/scripts`` sibling may back-import the router
spine (``_ensure_state_dir`` / ``_state_file``) lazily — the import-direction
fitness test governs only the ``src/teatree/hooks`` leaves.
"""

import contextlib
import sys
from pathlib import Path

from hooks.scripts.state_files import append_line, read_lines

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("subagent_no_commit", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.subagent_no_commit", sys.modules[__name__])


def _record_no_commit_signal(session_id: str, finding: object) -> None:
    r"""Persist + log one ``terminated_without_commit`` signal.

    Durable channel: append a deduped ``<branch>\t<worktree>`` line to the
    per-session ``<session>.no-commit`` state file (same shape/seam as the
    ``<session>.agents`` roster, which the PreCompact snapshot reads back).
    Live channel: a structured stderr line. Best-effort — a record failure
    must never propagate out of the Stop hook.
    """
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    branch = getattr(finding, "branch", "") or "(unknown)"
    worktree = getattr(finding, "worktree", "") or "(unknown)"
    print(  # noqa: T201 — hook stderr is the module's logging channel
        f"[hook_router] terminated_without_commit — sub-agent left work branch "
        f"{branch!r} at {worktree!r} with 0 commits; work would be lost on worktree teardown.",
        file=sys.stderr,
    )
    if not session_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        no_commit_file = _state_file(session_id, "no-commit")
        line = f"{branch}\t{worktree}"
        if line not in read_lines(no_commit_file):
            append_line(no_commit_file, line)


def handle_subagent_stop_no_commit(data: dict) -> None:
    """SubagentStop: record a work-branch worktree that produced 0 commits (#1205).

    Resolves the sub-agent's worktree from the harness ``cwd``, runs the
    conservative :func:`teatree.hooks.no_commit_detector.detect`, and records a
    ``terminated_without_commit`` signal only on the confirmed-flag verdict, so the
    orchestrator SEES the empty termination instead of assuming work landed. It is
    a pure DETECTION/surfacing hook — there is no recovery snapshot (the #1770
    capture mechanism was removed): unproven sub-agent work is surfaced for
    salvage, never auto-captured. No-op for a read-only/detached worktree, a
    committed branch, an undeterminable git state, or a missing ``cwd``.

    Crash-proof (#810 Stop contract): a broad boundary guard contains any
    unexpected error (an unimportable ``teatree``, git introspection failure)
    to a single stderr line — the sub-agent terminates normally and the
    detection is simply skipped (fail open).
    """
    try:
        worktree = data.get("cwd", "")
        if not worktree:
            return
        src_dir = Path(__file__).resolve().parents[2] / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from teatree.hooks import no_commit_detector  # noqa: PLC0415

        finding = no_commit_detector.detect(worktree)
        if finding.is_flagged:
            _record_no_commit_signal(data.get("session_id", ""), finding)
    except Exception as exc:  # noqa: BLE001 — SubagentStop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] no-commit detection skipped (unexpected error: {exc})",
            file=sys.stderr,
        )
