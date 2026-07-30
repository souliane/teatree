"""Session-to-session work hand-off.

Reuses the durable-state snapshot the PreCompact hook already builds (active
tickets, worktree paths/branches, in-flight sub-agents, open PRs,
approach/decisions, failing tests, loaded skills, t3-master status) — that
snapshot is the hand-off payload, so a hand-off and a post-compaction
recovery carry identical state. The hook writes it to
``${STATE_DIR}/t3-snapshot-<session>-precompact.md``; this module reads that
file as the payload, falling back to a payload DERIVED FROM LIVE DB STATE
(worktrees, active tickets, open PRs) when no snapshot exists yet — a session
that has not compacted still hands over its in-flight work (#3551).

The :class:`SessionHandover` DB row is the source of truth. The XDG file
mirror (``handover_mirror_path``) is for human-readability and for
bootstrapping a brand-new session whose process predates any DB read.

Target resolution (``create``):

- explicit ``to_session`` → that session.
- otherwise the LIVE ``t3-master`` slot holder (``t3 loop owner``).
- otherwise ``""`` — parked for whichever session starts next to claim.
"""

import contextlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from teatree.core.models.session_handover import SessionHandover

_SNAPSHOT_PREFIX = "t3-snapshot-"
_SNAPSHOT_SUFFIX = "-precompact.md"
_MIRROR_PREFIX = "handover-"
_MIRROR_SUFFIX = ".md"


def _state_dir() -> Path:
    """The dir the PreCompact hook writes snapshots into (mirrors ``hook_router.STATE_DIR``)."""
    return Path(
        os.environ.get(
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
            os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108 — fixed agent-controlled path, not user input
        )
    )


def _live_worktree_lines() -> list[str]:
    from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return [
        f"- `{worktree.branch or '(no branch)'}` — {worktree.worktree_path or '(no path)'} [{worktree.state}]"
        for worktree in Worktree.objects.exclude(state=Worktree.State.CREATED).order_by("pk")
    ]


def _live_ticket_lines() -> list[str]:
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return [
        f"- ticket {ticket.pk} ({ticket.short_description or ticket.issue_url or 'untitled'}) [{ticket.state}]"
        for ticket in Ticket.objects.exclude(state__in=Ticket.marker_release_states()).order_by("pk")
    ]


def _live_pr_lines() -> list[str]:
    from teatree.core.models import PullRequest  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return [
        f"- {pull_request.url or '(no url)'} ({pull_request.repo}!{pull_request.iid}) [{pull_request.state}]"
        for pull_request in PullRequest.objects.exclude(state=PullRequest.State.MERGED).order_by("pk")
    ]


@dataclass(frozen=True, slots=True)
class HandoverPayload:
    """The body one session hands over — the PreCompact snapshot, else live DB state.

    Two sources for one payload, composed here rather than left as three
    module functions each re-taking the same ``session_id``.
    """

    session_id: str

    def snapshot(self) -> str:
        """The PreCompact durable-state snapshot, or ``""``.

        ``""`` means no snapshot file exists (or it is unreadable) — :meth:`resolve`
        falls back to :meth:`live_state` rather than handing over a stub that tells
        the receiver to re-derive everything itself (#3551).
        """
        snapshot = _state_dir() / f"{_SNAPSHOT_PREFIX}{self.session_id}{_SNAPSHOT_SUFFIX}"
        try:
            return snapshot.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def live_state(self) -> str:
        """Derive a hand-off payload from live DB state — worktrees, tickets, PRs (#3551).

        The PreCompact snapshot is a convenience, not the only possible source:
        everything the payload contract promises is queryable at call time. A
        session that never compacted (or whose snapshot the hand-off cannot find)
        therefore still hands over something usable instead of a paragraph telling
        the receiver to re-derive it all. Returns ``""`` when there is genuinely
        nothing in flight, which the caller surfaces as a loud empty hand-off.
        """
        sections = (
            ("Worktrees", _live_worktree_lines()),
            ("Active tickets", _live_ticket_lines()),
            ("Open pull requests", _live_pr_lines()),
        )
        rendered = [f"## {title}\n\n" + "\n".join(lines) for title, lines in sections if lines]
        if not rendered:
            return ""
        header = (
            f"# Session hand-off — session `{self.session_id}` (derived from live state)\n\n"
            "No PreCompact snapshot was available, so this payload was derived from "
            "the DB at hand-off time: it carries the in-flight work but not the "
            "session's reasoning.\n"
        )
        return header + "\n\n" + "\n\n".join(rendered)

    def resolve(self) -> str:
        """The hand-off payload: the PreCompact snapshot, else live-derived state.

        ``""`` when neither source has anything — a hand-off with nothing durable
        to transfer, which :mod:`teatree.core.management.commands.handover` refuses
        loudly rather than reporting ``OK`` over an empty transfer (#3551).
        """
        return self.snapshot() or self.live_state()


def resolve_target_session(explicit_to: str) -> str:
    """Resolve the hand-off target: explicit id, else the live loop owner, else ``""``.

    ``""`` means "park for the next session to claim". The live loop owner
    is read via the same :class:`~teatree.core.models.LoopLease`
    ``t3-master`` slot the t3-master CLI uses, so a no-target hand-off
    lands on whichever session is actively driving the loop.
    """
    if explicit_to:
        return explicit_to
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    # The t3-master owner slot (``T3_MASTER_SLOT``); the tach boundary forbids
    # importing it here, so the literal is repeated at this read site.
    status = LoopLease.objects.ownership_status("t3-master")
    return status.owner_session if status.is_live else ""


def mirror_path() -> Path:
    """The configured XDG ``latest`` pointer for the most-recent hand-off.

    This is the stable, well-known path a human (or a bootstrapping session)
    reads to find the newest hand-off. The actual content lives in a
    per-session UNIQUE sibling file (:func:`unique_mirror_path`); this path is
    kept as a pointer to that newest file so concurrent hand-offs never clobber
    each other's content.
    """
    return get_effective_settings().handover_mirror_path


def _mirror_slug(value: str) -> str:
    """A filename-safe slug of a session id: ``[A-Za-z0-9._-]`` runs, collapsed."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "unknown"


def unique_mirror_path(handover: "SessionHandover", *, directory: Path) -> Path:
    """The collision-safe per-hand-off mirror file inside *directory*.

    Keyed on the ``from_session`` id AND the row's own ``created_at`` (a
    DB-assigned, deterministic timestamp — NOT wall-clock read at write time),
    so re-mirroring the same row is idempotent while two *different*
    concurrent hand-offs — from different sessions, or the same session at
    different instants — never resolve to the same file. This is the fix for
    the fixed-``latest.md`` clobber (directive #7).
    """
    stamp = handover.created_at.strftime("%Y%m%dT%H%M%S_%f")
    return directory / f"{_MIRROR_PREFIX}{_mirror_slug(handover.from_session)}-{stamp}{_MIRROR_SUFFIX}"


def newest_mirror(directory: Path) -> Path | None:
    """The most recent hand-off mirror in *directory*, or ``None`` when there is none.

    Mirror filenames embed the row's ``created_at`` as a fixed-width
    ``%Y%m%dT%H%M%S_%f`` stamp, so lexicographic order over the stamp IS
    chronological order — no filesystem mtime, which a copy or a container
    bind-mount rewrites.
    """
    mirrors = sorted(
        (p for p in directory.glob(f"{_MIRROR_PREFIX}*{_MIRROR_SUFFIX}") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.name.rsplit("-", 1)[-1],
    )
    return mirrors[-1] if mirrors else None


def _update_latest_pointer(pointer: Path, unique: Path) -> None:
    """Point the well-known ``latest`` path at the NEWEST mirror in its directory.

    Resolved from the directory's own contents rather than from whichever file
    was written last (#3563): a hand-off mirrored out of order — a replayed row,
    a second runtime writing into the same shared dir — must not drag ``latest``
    backwards onto an older session. Prefers a relative symlink so the pointer
    moves atomically; falls back to copying the content when the filesystem
    refuses symlinks. Best-effort: a pointer-update failure never loses the
    already-written unique content.
    """
    target = newest_mirror(unique.parent) or unique
    try:
        if pointer.is_symlink() or pointer.exists():
            if pointer.is_symlink() and pointer.readlink().name == target.name:
                return
            pointer.unlink()
        pointer.symlink_to(target.name)
    except OSError:
        with contextlib.suppress(OSError):
            shutil.copyfile(target, pointer)


def write_mirror(handover: "SessionHandover", path: Path | None = None) -> Path:
    """Mirror *handover* to a UNIQUE per-session file; repoint ``latest`` at it.

    *path* is the well-known ``latest`` pointer (default: :func:`mirror_path`).
    The content is written to a collision-safe sibling (:func:`unique_mirror_path`)
    so concurrent hand-offs from multiple sessions never clobber one another,
    and the ``latest`` pointer is moved to the newest file. Returns the UNIQUE
    content file (the durable artifact), not the pointer. A target of ``""``
    renders as ``next-session`` so the file always names a recipient.
    """
    pointer = path or mirror_path()
    directory = pointer.parent
    directory.mkdir(parents=True, exist_ok=True)
    unique = unique_mirror_path(handover, directory=directory)
    recipient = handover.to_session or "next-session"
    header = (
        f"# Session hand-off\n\n"
        f"- from: `{handover.from_session}`\n"
        f"- to: `{recipient}`\n"
        f"- created: {handover.created_at.isoformat()}\n\n"
        "---\n\n"
    )
    unique.write_text(header + handover.payload + "\n", encoding="utf-8")
    _update_latest_pointer(pointer, unique)
    return unique


def render_claimed_payload(claimed: "Sequence[SessionHandover]") -> str:
    """Concatenate every drained hand-off into one injectable payload (#3555).

    A single delivery may now carry several hand-offs (the parked queue is
    drained, not sampled), so each is fenced by a header naming its author and
    creation time — otherwise the receiving session reads N authors' state as
    one narrative. A lone hand-off renders as its bare payload, unchanged.
    """
    if len(claimed) == 1:
        return claimed[0].payload
    return "\n\n".join(
        f"## Hand-off {index} of {len(claimed)} — from `{row.from_session}` at {row.created_at.isoformat()}\n\n"
        f"{row.payload}"
        for index, row in enumerate(claimed, start=1)
    )


def claim_handovers(session_id: str) -> tuple[str, str]:
    """Drain every hand-off claimable by *session_id*; return ``(payload, origin)``.

    The single seam both pickup call sites use — the SessionStart hook and
    ``t3 <overlay> handover claim-on-start`` — so neither can drift back to a
    claim-one policy that strands the rest of the queue. ``origin`` names the
    handing session for one hand-off, or the session count for a drained batch.
    """
    from teatree.core.models import SessionHandover  # noqa: PLC0415 — deferred: ORM import needs the app registry

    claimed = SessionHandover.objects.claim_all(session_id) if session_id else []
    if not claimed:
        return "", ""
    origin = claimed[0].from_session if len(claimed) == 1 else f"{len(claimed)} sessions"
    return render_claimed_payload(claimed), origin


def create_handover(*, from_session: str, explicit_to: str) -> "tuple[SessionHandover, Path]":
    """Persist a hand-off from *from_session* and mirror it to the XDG file.

    Returns ``(handover_row, mirror_path)``. The payload is the reused
    PreCompact snapshot; the target is resolved per :func:`resolve_target_session`.
    """
    from teatree.core.models import SessionHandover  # noqa: PLC0415 — deferred: ORM import needs the app registry

    to_session = resolve_target_session(explicit_to)
    handover = SessionHandover.objects.create_handover(
        from_session=from_session,
        to_session=to_session,
        payload=HandoverPayload(from_session).resolve(),
    )
    return handover, write_mirror(handover)
