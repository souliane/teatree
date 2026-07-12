"""Per-session scoping of the dedicated ``loop:<name>`` layer (#1834 WI-2).

The default statusline loop line shows only the dedicated loops THIS session
owns; a foreign session's ``loop:<name>`` lease is subtracted. This module is
the statusline-side counterpart of :func:`teatree.loops.live.owned_per_loop_owners`
(the ``t3 loop list`` side) — both filter the same ``loop:<name>`` namespace by
owning session so the two surfaces can never drift.

Read-only. Scoping is **fail-open**: an anonymous / cron tick (empty
``current_session_id()``) or any DB read error keeps every per-loop chunk so a
broken read can never blank the statusline.
"""


def owned_per_loop_slots(session_id: str) -> set[str] | None:
    """Return the ``loop:<name>`` slots owned by ``session_id`` (#1834 WI-2).

    ``None`` is the **fail-open** sentinel: an empty ``session_id`` (a cron /
    anonymous tick that cannot resolve a session) and any DB read error both
    return ``None``, which the caller reads as "do not filter — show every
    per-loop chunk". A resolved session returns the set of ``loop:<name>``
    slot names it owns (possibly empty → every foreign per-loop chunk is
    subtracted). Read-only.
    """
    if not session_id:
        return None
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

        from teatree.core.loop_lease_manager import PER_LOOP_OWNER_PREFIX  # noqa: PLC0415 — tick-time import

        lease_model = apps.get_model("core", "LoopLease")
        rows = lease_model.objects.filter(name__startswith=PER_LOOP_OWNER_PREFIX, session_id=session_id).values_list(
            "name", flat=True
        )
        return set(rows)
    except Exception:  # noqa: BLE001 — best-effort resolution; a failure degrades to none
        return None


def current_session_owned_per_loop_slots() -> set[str] | None:
    """:func:`owned_per_loop_slots` resolved for ``current_session_id()`` (#1834 WI-2).

    The single entry point the statusline renderer calls so its
    ``loop:<name>`` chunk filter resolves the active session in one place;
    inherits the fail-open ``None`` sentinel from :func:`owned_per_loop_slots`
    (anonymous / cron tick, or DB read error).
    """
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415 — deferred: loaded at tick time

    return owned_per_loop_slots(current_session_id())


def per_loop_chunk_visible(name: str, owned_per_loop: set[str] | None) -> bool:
    """Whether a lease chunk is visible to this session (#1834 WI-2).

    Infra leases (``loop-tick`` and friends, which use ``-`` not ``:``) are
    always visible. A ``loop:<name>`` lease is visible iff this session owns
    it; ``owned_per_loop is None`` is the fail-open marker (no resolvable
    session / read error) under which every per-loop chunk stays visible.
    """
    from teatree.core.loop_lease_manager import is_per_loop_owner_slot  # noqa: PLC0415 — deferred: loaded at tick time

    if not is_per_loop_owner_slot(name):
        return True
    if owned_per_loop is None:
        return True
    return name in owned_per_loop


def is_per_loop_owner_slot(name: str) -> bool:
    """Whether ``name`` is a per-loop owner slot (``loop:<name>``) — loop-side re-export.

    Thin passthrough to :func:`teatree.core.loop_lease_manager.is_per_loop_owner_slot`
    so the statusline (:mod:`teatree.loop.statusline_loops`) reads the predicate through
    the loop layer — the tach graph forbids it importing ``teatree.core.loop_lease_manager``
    directly, but ``teatree.loop.loop_scoping`` already may.
    """
    from teatree.core.loop_lease_manager import is_per_loop_owner_slot as _impl  # noqa: PLC0415 — deferred

    return _impl(name)


def is_transient_tick_mutex(name: str) -> bool:
    """Whether a lease ``name`` is a transient per-loop tick mutex (``loop-tick:<name>``).

    The single-loop tick (``t3 loops tick --loop <name>``) holds a
    ``loop-tick:<name>`` mutex for the duration of its beat purely to serialise
    concurrent ticks of the SAME loop. It is NOT a user-facing loop, and while
    it is held the matching durable ``loop:<name>`` owner lease is held too — so
    rendering it produces a confusing duplicate where the currently-ticking loop
    shows under both ``tick:<name>`` (the stripped mutex) and ``loop:<name>``
    (the owner lease). The statusline loop line therefore drops it. The bare
    master ``loop-tick`` mutex (no trailing ``:``) is left visible as ``tick``.
    """
    from teatree.core.loop_lease_manager import is_per_loop_tick_mutex  # noqa: PLC0415 — deferred: loaded at tick time

    return is_per_loop_tick_mutex(name)


__all__ = [
    "current_session_owned_per_loop_slots",
    "is_per_loop_owner_slot",
    "is_transient_tick_mutex",
    "owned_per_loop_slots",
    "per_loop_chunk_visible",
]
