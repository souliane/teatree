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
        from django.apps import apps  # noqa: PLC0415

        from teatree.core.loop_lease_manager import PER_LOOP_OWNER_PREFIX  # noqa: PLC0415

        lease_model = apps.get_model("core", "LoopLease")
        rows = lease_model.objects.filter(name__startswith=PER_LOOP_OWNER_PREFIX, session_id=session_id).values_list(
            "name", flat=True
        )
        return set(rows)
    except Exception:  # noqa: BLE001
        return None


def current_session_owned_per_loop_slots() -> set[str] | None:
    """:func:`owned_per_loop_slots` resolved for ``current_session_id()`` (#1834 WI-2).

    The single entry point the statusline renderer calls so its
    ``loop:<name>`` chunk filter resolves the active session in one place;
    inherits the fail-open ``None`` sentinel from :func:`owned_per_loop_slots`
    (anonymous / cron tick, or DB read error).
    """
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    return owned_per_loop_slots(current_session_id())


def per_loop_chunk_visible(name: str, owned_per_loop: set[str] | None) -> bool:
    """Whether a lease chunk is visible to this session (#1834 WI-2).

    Infra leases (``loop-tick`` and friends, which use ``-`` not ``:``) are
    always visible. A ``loop:<name>`` lease is visible iff this session owns
    it; ``owned_per_loop is None`` is the fail-open marker (no resolvable
    session / read error) under which every per-loop chunk stays visible.
    """
    from teatree.core.loop_lease_manager import is_per_loop_owner_slot  # noqa: PLC0415

    if not is_per_loop_owner_slot(name):
        return True
    if owned_per_loop is None:
        return True
    return name in owned_per_loop


__all__ = ["current_session_owned_per_loop_slots", "owned_per_loop_slots", "per_loop_chunk_visible"]
