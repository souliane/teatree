"""The SessionStart hand-off pickup — drain the parked queue into a starting session.

Extracted from ``hook_router`` (the shrink-only god module) so the hand-off
concern is one self-describing unit: the DB drain, its file-mirror fallback, and
the directive text a claimed hand-off becomes.

Failing open is the contract — a hand-off pickup must never block the
SessionStart directive — but failing open is not licence to fail SILENTLY
(#3810). Six hand-offs sat unclaimed for a week on a live box because both
degradations here left no trace at all: an unimportable Django and a raising
``claim_handovers`` each set ``payload = ""``, so a queue that never drained was
indistinguishable from a queue that was always empty. Every degradation now logs
a WARNING naming the session and the cause, then carries on exactly as before.
"""

import contextlib
import logging
import sys
from pathlib import Path

from hooks.scripts.django_bootstrap import bootstrap_teatree_django

#: With no logging configured in a hook subprocess, ``logging``'s lastResort
#: handler writes WARNING and above to stderr — the hook layer's logging channel.
logger = logging.getLogger("teatree.hook_router")

_HANDOVER_DIRECTIVE = (
    "SESSION HAND-OFF RECEIVED{origin} — another session handed its full "
    "in-flight work to you. Read the durable-state snapshot below, then "
    "resume that work (re-derive identity, worktrees, open PRs, and the "
    "next action):\n\n{payload}"
)


def claim_session_handover(session_id: str) -> str | None:
    """Claim every unclaimed hand-off for *session_id* as a directive, or ``None``.

    The zero-copy-paste takeover: a fresh / non-owner session picks up the
    hand-offs targeted AT it or parked for "next session" from the
    ``SessionHandover`` DB table (the source of truth), marks them claimed so
    they inject exactly once, and returns the payload to merge into the
    SessionStart ``additionalContext``. Falls back to the XDG file mirror when
    the DB is unreachable (a brand-new session whose process predates a readable
    DB), then to ``None``.
    """
    payload = ""
    from_session = ""
    if not session_id:
        logger.warning("session hand-off drain skipped: no session id on the SessionStart payload")
        return None
    if bootstrap_teatree_django():
        try:
            from teatree.core.handover import claim_handovers  # noqa: PLC0415 — deferred: ORM/app-registry

            payload, from_session = claim_handovers(session_id)
        except Exception:
            logger.warning(
                "session hand-off drain FAILED for session %s — failing open to the file mirror; "
                "any parked hand-off stays unclaimed until this is fixed",
                session_id,
                exc_info=True,
            )
            payload = ""
    else:
        logger.warning(
            "session hand-off drain SKIPPED for session %s: teatree/Django is not importable by this hook "
            "interpreter, so the SessionHandover queue cannot be read; falling back to the file mirror",
            session_id,
        )

    if not payload:
        payload, from_session = claim_session_handover_from_file()
    if not payload:
        return None
    origin = f" from session `{from_session}`" if from_session else ""
    return _HANDOVER_DIRECTIVE.format(origin=origin, payload=payload)


def claim_session_handover_from_file() -> tuple[str, str]:
    """Read the XDG mirror as a one-shot hand-off fallback, renaming it on claim.

    Returns ``(payload, from_session)`` or ``("", "")``. The mirror is the
    bootstrap path for a brand-new session that cannot reach the DB. To keep
    the file single-use (mirroring the DB ``claimed_at`` once-only contract)
    the claimed file is renamed to ``latest.claimed.md`` so a re-fired
    SessionStart does not re-inject it.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        # ``handover_mirror_path`` is DB-home. Read it Django-free via ``cold_reader``
        # (the canonical sqlite); an absent row / unreachable DB fails open through
        # the parser to the default bootstrap path.
        from teatree.config import _parse_handover_mirror_path, cold_reader  # noqa: PLC0415, PLC2701 — cold-hook import

        path = _parse_handover_mirror_path(cold_reader.str_setting("handover_mirror_path", default=""))
        text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        if not text:
            return "", ""
        with contextlib.suppress(OSError):
            path.replace(path.with_name("latest.claimed.md"))
    except Exception:
        logger.warning("session hand-off file-mirror read failed — no hand-off delivered", exc_info=True)
        return "", ""
    else:
        return text, ""
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


__all__ = ["claim_session_handover", "claim_session_handover_from_file"]
