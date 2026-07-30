"""Is a loop lease live, and is its owner foreign? — the ORM-free decision layer.

Split from the sibling ``loop_lease_manager`` (the ``LoopLease`` queryset/manager):
these are pure predicates over a lease's ``(session_id, owner_pid, expires_at)``
triple plus the caller's slot policy, so they are decided and tested without a row.

``trust_pid_past_ttl`` is the slot policy the manager supplies. The GLOBAL
``t3-master`` slot passes ``True``: an alive ``owner_pid`` keeps the lease live past
its TTL, because a busy owner fires no self-pump so no tick re-claims (#1604). A
``loop:<name>`` PER-LOOP slot passes ``False`` (#3571): a dead session's pid is
routinely reused / cross-namespace, so once its TTL lapses the lease is reclaimable
regardless of pid liveness.
"""

from collections.abc import Callable
from datetime import datetime


def pid_alive_probe() -> Callable[[int], bool] | None:
    """``teatree.utils.singleton.pid_alive``, or ``None`` when it cannot be imported.

    One deferred-import seam every liveness call site shares, so an environment
    without the probe degrades identically everywhere (indeterminate → the TTL
    backstop) instead of drifting per call site.
    """
    try:
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415 — deferred: call-time import, kept lazy
    except ImportError:
        return None
    return pid_alive


def anchorable_owner_pid(owner_pid: int | None) -> int | None:
    """``owner_pid`` unless it is PROVABLY dead, in which case ``None`` (#3646).

    A lease may only be anchored on a process that is actually running. A caller
    resolving its durable session pid from a stale source — the loop registry
    record left behind by a REPLACED worker — hands in a pid that died with that
    worker; persisting it makes every subsequent reclaim sweep read the LIVE
    holder's own row as dead-owned, evict it, and log the reclaim again on the
    next tick, forever. Dropping the dead pid to ``None`` records the live
    session as the owner under the TTL backstop (the fallback release), so the
    reclaim happens exactly once.
    """
    if owner_pid is None:
        return None
    probe = pid_alive_probe()
    if probe is not None and not probe(owner_pid):
        return None
    return owner_pid


def lease_is_live(
    session_id: str,
    owner_pid: int | None,
    expires_at: datetime | None,
    now: datetime,
    *,
    trust_pid_past_ttl: bool,
) -> bool:
    """Whether a non-empty session's lease is live (#1073/#1604/#3571).

    The single liveness predicate every caller shares so they can never drift. A
    determinately-DEAD ``owner_pid`` is NOT live at ANY TTL. An ALIVE ``owner_pid``
    past an expired TTL depends on ``trust_pid_past_ttl`` — the #3571 crux, since a
    dead session's pid is routinely reused / cross-namespace so an alive pid is not
    proof the session is alive: ``True`` (``t3-master``) keeps it live past TTL (the
    #1604 busy-owner protection); ``False`` (a ``loop:<name>`` slot) falls through to
    the TTL so a lapsed TTL is reclaimable while a fresh TTL still reads live. An
    INDETERMINATE pid (null, or ``pid_alive`` unavailable) fails CLOSED to the TTL.
    An empty ``session_id`` is never live.
    """
    if not session_id:
        return False
    if owner_pid is not None:
        pid_alive = pid_alive_probe()
        if pid_alive is not None:
            if not pid_alive(owner_pid):
                return False
            if trust_pid_past_ttl:
                return True
            # Per-loop slot: an alive-but-possibly-reused pid does not extend
            # liveness past the TTL; fall through to the TTL backstop.
    return expires_at is not None and expires_at > now


def live_foreign_owner_session(row: dict | None, session_id: str, now: datetime, *, trust_pid_past_ttl: bool) -> str:
    """The non-empty session of a live owner *other than* ``session_id``, or ``""``.

    Live is the slot-aware :func:`lease_is_live` verdict; the same session refreshing
    its own claim is never "foreign". Returns ``""`` when the slot is unowned, owned
    by ``session_id`` itself, or held by a dead/expired (reclaimable) owner.
    """
    owner_session = (row or {}).get("session_id") or ""
    if owner_session == session_id:
        return ""
    is_live = lease_is_live(
        owner_session,
        (row or {}).get("owner_pid"),
        (row or {}).get("lease_expires_at"),
        now,
        trust_pid_past_ttl=trust_pid_past_ttl,
    )
    return owner_session if is_live else ""


def pid_is_foreign(stored_pid: int | None, current_pid: int | None) -> bool:
    """Whether a live lease's ``owner_pid`` belongs to a DIFFERENT OS process (#1604).

    A live foreign-session lease whose ``owner_pid`` matches ``current_pid`` is a
    post-compaction same-process self-reclaim — the session rotated its id but the OS
    process is ours — so it is NOT a genuinely foreign owner. A null stored pid is
    treated as foreign (unknown → bias to report-foreign/KEEP).
    """
    return current_pid is None or stored_pid != current_pid
