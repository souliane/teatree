"""Session-to-session work hand-off.

Reuses the durable-state snapshot the PreCompact hook already builds (active
tickets, worktree paths/branches, in-flight sub-agents, open PRs,
approach/decisions, failing tests, loaded skills, t3-master status) — that
snapshot is the hand-off payload, so a hand-off and a post-compaction
recovery carry identical state. The hook writes it to
``${STATE_DIR}/t3-snapshot-<session>-precompact.md``; this module reads that
file as the payload, falling back to a minimal stub when no snapshot exists
yet (a session that has not compacted).

The :class:`SessionHandover` DB row is the source of truth. The XDG file
mirror (``handover_mirror_path``) is for human-readability and for
bootstrapping a brand-new session whose process predates any DB read.

Target resolution (``create``):

- explicit ``to_session`` → that session.
- otherwise the LIVE ``t3-master`` slot holder (``t3 loop owner``).
- otherwise ``""`` — parked for whichever session starts next to claim.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from teatree.core.models.session_handover import SessionHandover

_SNAPSHOT_PREFIX = "t3-snapshot-"
_SNAPSHOT_SUFFIX = "-precompact.md"


def _state_dir() -> Path:
    """The dir the PreCompact hook writes snapshots into (mirrors ``hook_router.STATE_DIR``)."""
    return Path(
        os.environ.get(
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
            os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108
        )
    )


def snapshot_payload(session_id: str) -> str:
    """Return the durable-state snapshot for *session_id*, or a minimal stub.

    Reads the file the PreCompact hook already wrote. A session that has
    never compacted has no snapshot file; rather than fail the hand-off,
    return a short stub naming the session so the receiving session at
    least knows where the work came from and can ask.
    """
    snapshot = _state_dir() / f"{_SNAPSHOT_PREFIX}{session_id}{_SNAPSHOT_SUFFIX}"
    try:
        text = snapshot.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    return (
        f"# Session hand-off — session `{session_id}`\n\n"
        "No PreCompact durable-state snapshot existed for this session yet "
        "(it had not compacted). Re-derive the in-flight work from the "
        "worktrees, open PRs, and active tickets before continuing."
    )


def resolve_target_session(explicit_to: str) -> str:
    """Resolve the hand-off target: explicit id, else the live loop owner, else ``""``.

    ``""`` means "park for the next session to claim". The live loop owner
    is read via the same :class:`~teatree.core.models.LoopLease`
    ``t3-master`` slot the t3-master CLI uses, so a no-target hand-off
    lands on whichever session is actively driving the loop.
    """
    if explicit_to:
        return explicit_to
    from teatree.core.models import LoopLease  # noqa: PLC0415

    # The t3-master owner slot (``T3_MASTER_SLOT``); the tach boundary forbids
    # importing it here, so the literal is repeated at this read site.
    status = LoopLease.objects.ownership_status("t3-master")
    return status.owner_session if status.is_live else ""


def mirror_path() -> Path:
    """The configured XDG file mirror path for the latest hand-off."""
    return get_effective_settings().handover_mirror_path


def write_mirror(handover: "SessionHandover", path: Path | None = None) -> Path:
    """Mirror *handover* to the human-readable XDG file (overwrites the single ``latest.md``).

    Best-effort framing: the parent dir is created. A target of ``""``
    renders as ``next-session`` so the file always names a recipient.
    """
    target = path or mirror_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    recipient = handover.to_session or "next-session"
    header = (
        f"# Session hand-off\n\n"
        f"- from: `{handover.from_session}`\n"
        f"- to: `{recipient}`\n"
        f"- created: {handover.created_at.isoformat()}\n\n"
        "---\n\n"
    )
    target.write_text(header + handover.payload + "\n", encoding="utf-8")
    return target


def create_handover(*, from_session: str, explicit_to: str) -> "tuple[SessionHandover, Path]":
    """Persist a hand-off from *from_session* and mirror it to the XDG file.

    Returns ``(handover_row, mirror_path)``. The payload is the reused
    PreCompact snapshot; the target is resolved per :func:`resolve_target_session`.
    """
    from teatree.core.models import SessionHandover  # noqa: PLC0415

    to_session = resolve_target_session(explicit_to)
    handover = SessionHandover.objects.create_handover(
        from_session=from_session,
        to_session=to_session,
        payload=snapshot_payload(from_session),
    )
    return handover, write_mirror(handover)
