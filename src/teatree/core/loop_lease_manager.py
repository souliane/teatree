"""Manager/queryset for the machine-wide ``LoopLease`` rows (#1073/#786/#54).

Split out of ``teatree.core.managers`` so the loop-owner claim concern —
the pid-anchored ``claim_ownership`` CAS, the conditional ``evict_stale_owner``
decision table, and the read-only ``OwnershipStatus`` snapshot — lives in
one self-describing module. ``teatree.core.managers`` re-exports the public
symbols so existing ``from teatree.core.managers import …`` call sites are
unchanged.

Loop-owner liveness is PID-ANCHORED, not TTL-anchored: an owner that is
alive but BUSY past the tick TTL fires no Stop self-pump, so no tick
re-claims and its lease TTL-lapses while the owner process is still alive.
``claim_ownership`` therefore treats a non-empty owner whose ``owner_pid``
is alive as a LIVE owner — protected past its TTL against any non-
``take_over`` claim. The TTL is the FALLBACK release, used only when
``owner_pid`` is null or dead. The doctrine has no ``renew()`` and no
background timer (#54): the per-tick re-claim IS the heartbeat.
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from django.db import models
from django.db.models import Q
from django.utils import timezone

#: The single machine-wide loop-owner slot (#1073). This is the DEFAULT
#: that the fat ``loop_tick`` gate claims; its pid-anchored, hijack-guarded
#: semantics are unchanged. Per-loop owners live in the ``loop:<name>``
#: namespace below — a disjoint key space, so a per-loop claim can never
#: collide with or evict the global owner.
GLOBAL_OWNER_SLOT = "loop-owner"

#: Prefix for the additive per-loop owning-session layer (#1834). A
#: dedicated loop (PR#3) claims ``loop:<name>`` (e.g. ``loop:dispatch``)
#: so two dedicated loops can be owned by two different sessions
#: concurrently. The prefix keeps the per-loop keys in their own namespace,
#: disjoint from ``GLOBAL_OWNER_SLOT`` and from the infra-slot leases
#: (``loop-tick`` / ``loop-self-improve`` / …), which use ``-`` not ``:``.
PER_LOOP_OWNER_PREFIX = "loop:"


def per_loop_owner_slot(loop_name: str) -> str:
    """Canonical owner-slot key for a dedicated per-loop owning session (#1834).

    The fully-qualified form ``loop:<loop_name>`` is the canonical key —
    every per-loop claim/read/compare normalizes UP to it at the boundary
    so a bare ``dispatch`` and a qualified ``loop:dispatch`` can never be
    treated as two different slots. The global single-owner slot is the
    reserved :data:`GLOBAL_OWNER_SLOT` constant, never produced by this
    function, so the two layers occupy disjoint key space.

    An already-qualified ``loop:dispatch`` is returned unchanged
    (idempotent), so call sites may pass either the bare loop name or the
    qualified slot without double-prefixing.
    """
    name = loop_name.strip()
    if name.startswith(PER_LOOP_OWNER_PREFIX):
        return name
    return f"{PER_LOOP_OWNER_PREFIX}{name}"


def is_per_loop_owner_slot(slot: str) -> bool:
    """Whether ``slot`` is a per-loop owner key (``loop:<name>``), not the global one."""
    return slot.startswith(PER_LOOP_OWNER_PREFIX)


class OwnershipStatus(NamedTuple):
    """Read-only snapshot of a session-scoped loop-owner claim (#1073/#1604).

    ``is_live`` is the predicate callers branch on. It is pid-anchored
    (matching ``claim_ownership``'s liveness): ``True`` iff a non-empty
    session holds a claim that is either unexpired OR whose ``owner_pid``
    is still alive, keyed on ``session_id`` rather than ``owner``.
    """

    owner_session: str
    expires_at: datetime | None
    is_live: bool


class LoopLeaseQuerySet(models.QuerySet):
    def claim_ownership(
        self,
        name: str,
        *,
        session_id: str,
        owner_pid: int | None = None,
        ttl_seconds: int = 1800,
        take_over: bool = False,
    ) -> tuple[bool, str]:
        """Claim/refresh the persistent session-scoped loop-owner row (#1073).

        Returns ``(won, current_owner_session)``. Ownership liveness is
        PID-ANCHORED: a live owner is a non-empty ``session_id`` whose lease
        is live, where "live" means ``lease_expires_at > now`` OR
        ``pid_alive(owner_pid)``. An alive owner process is therefore
        protected past its tick TTL — the invariant is "the loop stays with
        the existing session; it transfers ONLY on that session's process
        termination or an explicit ``take_over``." The TTL is the FALLBACK
        release used only when ``owner_pid`` is null or dead. There is still
        NO ``renew()`` and no background timer (#54 doctrine preserved): the
        per-tick re-claim IS the heartbeat.

        ``take_over=False`` (the per-tick path / the heartbeat). An
        anonymous caller (``session_id == ""``) NEVER persists a row: it
        RUNS (``won=True``) iff there is no live owner and otherwise SKIPs
        (``won=False``). Pure-cron / no-session deployments (#1107) still
        run the tick when unowned, but the phantom "owned by nobody but not
        expired" row can never form and a live owner's row is never erased.
        A pid-anchored reclaim then applies: a lease whose ``owner_pid`` is
        ALIVE and whose ``session_id`` is a *different* non-empty session
        BLOCKS the claim even if its TTL has lapsed. Otherwise the existing
        backend-agnostic conditional-UPDATE CAS runs (correct on the
        production SQLite backend where ``select_for_update`` is a silent
        no-op — the #786 B1 lesson): the ``WHERE`` matches only when the
        claim is unclaimed (``session_id=""``), already this session's
        (refresh), or stale (expired / never set), so a concurrent refresh
        between our read and the write is still guarded against.

        ``take_over=True`` (the user hand-off — ``t3 loop claim
        --take-over``): an unconditional UPDATE on ``name`` that evicts
        even a live claimant, so the chat-only user can wrest the loop
        back from a hijacking session within one tick.

        On a win the row's ``session_id``/``acquired_at``/
        ``lease_expires_at``/``owner_pid`` are set. The returned
        ``current_owner_session`` is read back *after* the write so a
        loser reports WHO actually holds it.

        ``owner_pid`` (#1604): the durable session process id (the long-lived
        session process, not the ephemeral hook/tick subprocess). Stored on
        win so ``evict_stale_owner`` can distinguish a post-compaction
        same-process self-reclaim (same pid → safe to evict) from a
        genuinely different live session (different live pid → KEEP).
        Callers that cannot resolve the session pid pass ``None``; the
        stored null is treated conservatively as "unknown → KEEP" (INV4).
        """
        now = timezone.now()
        expires = now + timedelta(seconds=ttl_seconds)
        self.get_or_create(name=name)

        if take_over:
            self.filter(name=name).update(
                session_id=session_id,
                owner_pid=owner_pid,
                acquired_at=now,
                lease_expires_at=expires,
            )
            current = self.filter(name=name).values_list("session_id", flat=True).first() or ""
            return True, current

        row = self.filter(name=name).values("session_id", "owner_pid", "lease_expires_at").first()
        live_owner = self._live_foreign_owner_session(row, session_id, now)

        if not session_id:
            # An anonymous caller never persists ownership. It RUNS iff
            # there is no live owner (so pure-cron deployments still tick),
            # and never erases a live owner's row.
            return not live_owner, live_owner

        if live_owner:
            # A protected live foreign owner (alive pid, or unexpired TTL)
            # blocks the claim — no DB write.
            return False, live_owner

        won = (
            self.filter(name=name)
            .filter(
                Q(session_id="")
                | Q(session_id=session_id)
                | Q(lease_expires_at__isnull=True)
                | Q(lease_expires_at__lte=now)
            )
            .update(
                session_id=session_id,
                owner_pid=owner_pid,
                acquired_at=now,
                lease_expires_at=expires,
            )
        )
        current = self.filter(name=name).values_list("session_id", flat=True).first() or ""
        return won == 1, current

    @staticmethod
    def _session_lease_is_live(
        session_id: str, owner_pid: int | None, expires_at: datetime | None, now: datetime
    ) -> bool:
        """Whether a non-empty session's lease is live (pid-anchored, #1073/#1604).

        The single liveness predicate shared by every caller so the three
        (``claim_ownership``/``_live_foreign_owner_session``,
        ``ownership_status``, and the foreign-owner block) can never drift.
        A lease is live iff its ``session_id`` is non-empty AND either its
        TTL is unexpired (``expires_at > now``) OR its ``owner_pid`` is
        alive. ``pid_alive`` is imported lazily and on ``ImportError`` the
        pid branch fails open to not-live (reclaimable) — safe because the
        pid branch is only consulted once the TTL has already lapsed.
        """
        if not session_id:
            return False
        if expires_at is not None and expires_at > now:
            return True
        if owner_pid is None:
            return False
        try:
            from teatree.utils.singleton import pid_alive  # noqa: PLC0415
        except ImportError:
            return False
        return pid_alive(owner_pid)

    @classmethod
    def _live_foreign_owner_session(cls, row: dict | None, session_id: str, now: datetime) -> str:
        """The non-empty session of a live owner *other than* ``session_id``, or ``""``.

        A live owner is pid-anchored via :meth:`_session_lease_is_live`: its
        lease is unexpired (``lease_expires_at > now``) OR its ``owner_pid``
        is alive. The same session refreshing its own claim is never
        "foreign". Returns ``""`` when the slot is unowned, owned by
        ``session_id`` itself, or owned by a dead/null-pid + expired owner
        (reclaimable). A null ``owner_pid`` is decided by the TTL check
        alone; a ``pid_alive`` ``ImportError`` fails open to reclaimable
        (treated as not-live), which is safe because the pid branch is gated
        behind an already-lapsed TTL.
        """
        owner_session = (row or {}).get("session_id") or ""
        if owner_session == session_id:
            return ""
        is_live = cls._session_lease_is_live(
            owner_session, (row or {}).get("owner_pid"), (row or {}).get("lease_expires_at"), now
        )
        return owner_session if is_live else ""

    def evict_stale_owner(
        self,
        name: str,
        *,
        keep_session_id: str,
        current_pid: int | None,
    ) -> int:
        """Evict the ``name`` lease iff it is safe to do so (#1604/#1675).

        Decision table (INV1 / INV4 / #786 B1 backend-agnostic CAS).
        Liveness is PID-ANCHORED via :meth:`_session_lease_is_live` — the
        same predicate ``claim_ownership`` and ``ownership_status`` use —
        so an owner that is alive but BUSY past its tick TTL is a LIVE
        owner here too and is never blanked:

        - Dead (expired TTL **and** null/dead owner_pid): EVICT (truly dead).
        - Live (unexpired TTL **or** alive owner_pid) + same pid: EVICT
            (post-compaction same-process self-reclaim; session rotated its
            id — the pid match is the safety condition, regardless of TTL).
        - Live + null owner_pid: KEEP (unknown process, INV4 bias).
        - Live + alive owner_pid != current_pid: KEEP (INV1, foreign lease).

        The final UPDATE re-asserts the safety condition in its ``WHERE``
        clause (backend-agnostic CAS) so a concurrent tick that refreshed
        the lease between our read and this write is not evicted.

        Returns the number of rows orphaned (0 or 1).
        """
        try:
            from teatree.utils.singleton import pid_alive  # noqa: PLC0415
        except ImportError:
            pid_alive = None  # type: ignore[assignment]

        now = timezone.now()
        candidates = self.filter(name=name).exclude(session_id=keep_session_id)
        row = candidates.values("session_id", "owner_pid", "lease_expires_at").first()
        if not row or not (row["session_id"] or ""):
            return 0

        expires_at = row["lease_expires_at"]
        stored_pid = row["owner_pid"]
        is_live = self._session_lease_is_live(row["session_id"], stored_pid, expires_at, now)

        if not is_live:
            return candidates.filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).update(
                session_id="", owner_pid=None, acquired_at=None, lease_expires_at=None
            )

        if stored_pid is None:
            return 0

        if current_pid is not None and stored_pid == current_pid:
            return (
                self.filter(name=name, owner_pid=stored_pid)
                .exclude(session_id=keep_session_id)
                .update(session_id="", owner_pid=None, acquired_at=None, lease_expires_at=None)
            )

        if pid_alive is not None and not pid_alive(stored_pid):
            return (
                self.filter(name=name, owner_pid=stored_pid)
                .exclude(session_id=keep_session_id)
                .update(session_id="", owner_pid=None, acquired_at=None, lease_expires_at=None)
            )

        return 0

    def heartbeat_ownership(self, name: str, *, session_id: str, ttl_seconds: int = 1800) -> bool:
        """Extend the loop-owner lease IFF this session still holds it (#1073).

        CAS on ``session_id``: a row another session took over (or that
        expired and was reclaimed) no longer matches, so this returns
        ``False`` and the caller learns it is no longer the owner. The
        per-tick :meth:`claim_ownership` already subsumes this for the
        loop-tick path; ``heartbeat_ownership`` is the explicit-refresh
        primitive for callers that want to extend without re-evaluating
        the take-over policy.
        """
        now = timezone.now()
        refreshed = self.filter(name=name, session_id=session_id).update(
            lease_expires_at=now + timedelta(seconds=ttl_seconds),
        )
        return refreshed == 1

    def ownership_status(self, name: str) -> OwnershipStatus:
        """Read-only snapshot of the named loop-owner claim (#1073/#1604).

        ``is_live`` is pid-anchored via :meth:`_session_lease_is_live`: it
        is ``True`` iff a non-empty session holds a claim that is either
        unexpired (``lease_expires_at > now``) OR whose ``owner_pid`` is
        still alive — so the snapshot does not go blind during the busy-
        owner-past-TTL window the #1604 fix targets. A missing row reports
        ``("", None, False)`` — unclaimed.
        """
        row = self.filter(name=name).values("session_id", "lease_expires_at", "owner_pid").first()
        if row is None:
            return OwnershipStatus(owner_session="", expires_at=None, is_live=False)
        session = row["session_id"] or ""
        expires_at = row["lease_expires_at"]
        is_live = self._session_lease_is_live(session, row["owner_pid"], expires_at, timezone.now())
        return OwnershipStatus(owner_session=session, expires_at=expires_at, is_live=is_live)

    def release_ownership(self, name: str, *, session_id: str) -> bool:
        """Release the loop-owner claim iff held by ``session_id`` (CAS).

        A non-owner release is a no-op (0 rows) so it can never evict a
        live owner — the chat-only user's ``t3 loop release`` only ever
        clears its *own* session's claim.
        """
        released = (
            self.filter(name=name, session_id=session_id)
            .exclude(session_id="")
            .update(
                session_id="",
                acquired_at=None,
                lease_expires_at=None,
            )
        )
        return released == 1

    def acquire(self, name: str, *, owner: str, lease_seconds: int = 120) -> bool:
        """Atomically acquire/renew the named loop lease (#786 WS2).

        Backend-agnostic compare-and-swap: a single conditional ``UPDATE``
        whose ``WHERE`` matches only when the lease is unowned, already
        held by *this* owner (renew), or expired. Exactly one of N
        concurrent ticks updates 1 row and wins; the losers update 0 rows
        and return ``False``. NOT ``select_for_update(skip_locked=True)``
        — that is a silent no-op on the production SQLite backend
        (``has_select_for_update_skip_locked`` is ``False``; the #786 B1
        lesson). The row is created on first contact via ``get_or_create``
        so a missing lease is indistinguishable from an expired one.
        Returns ``True`` iff this caller now holds the lease.
        """
        now = timezone.now()
        expires = now + timedelta(seconds=lease_seconds)
        self.get_or_create(name=name)
        won = (
            self.filter(name=name)
            .filter(Q(owner="") | Q(owner=owner) | Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .update(
                owner=owner,
                acquired_at=now,
                lease_expires_at=expires,
            )
        )
        return won == 1

    def release(self, name: str, *, owner: str) -> bool:
        """Release the lease iff held by ``owner`` (CAS on owner).

        A non-owner release is a no-op (0 rows) so a losing tick can never
        evict the live owner. Returns ``True`` iff this owner released it.
        """
        released = self.filter(name=name, owner=owner).update(
            owner="",
            acquired_at=None,
            lease_expires_at=None,
        )
        return released == 1


LoopLeaseManager = models.Manager.from_queryset(LoopLeaseQuerySet)
