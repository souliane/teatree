"""Manager/queryset for the machine-wide ``LoopLease`` rows (#1073/#786/#54).

Split out of ``teatree.core.managers`` so the t3-master claim concern —
the pid-anchored ``claim_ownership`` CAS, the conditional ``evict_stale_owner``
decision table, and the read-only ``OwnershipStatus`` snapshot — lives in
one self-describing module. ``teatree.core.managers`` re-exports the public
symbols so existing ``from teatree.core.managers import …`` call sites are
unchanged.

Liveness is slot-aware via ``lease_is_live``'s ``trust_pid_past_ttl``.
The GLOBAL ``t3-master`` slot is PID-ANCHORED: an alive ``owner_pid`` keeps the
lease live past its TTL (a busy owner fires no self-pump so no tick re-claims),
transferring only on process death or a ``--take-over`` — the TTL is the
fallback release, and there is no ``renew()`` / background timer (#54): the
per-tick re-claim IS the heartbeat. A ``loop:<name>`` PER-LOOP slot does NOT
trust ``pid_alive`` past its TTL (#3571): a dead session's pid is routinely
reused / cross-namespace, so once its TTL lapses the lease is reclaimable
regardless of pid liveness (the per-tick re-claim is that session's heartbeat),
while a fresh TTL still reads live — preserving the duplicate-worker guard
(#3534). ``reclaim_dead_owner_leases`` runs that reclaim on a cadence from the
worker supervisor, ``run_boot_sweeps`` (``t3 recover``), and the self-heal
watchdog. A same-process self-reclaim across a session-id rotation (#2835:
compaction rotates the id but not the process) re-anchors either slot's lease
to the new id and wins.
"""

import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from django.db import models
from django.db.models import F, Q
from django.db.models.expressions import Combinable
from django.utils import timezone

from teatree.core.loop_lease_liveness import (
    anchorable_owner_pid,
    lease_is_live,
    live_foreign_owner_session,
    pid_alive_probe,
    pid_is_foreign,
)

logger = logging.getLogger(__name__)

#: The single machine-wide t3-master owner lease slot — the global owner
#: lease whose holder IS the t3 master (autonomous-lane redesign §8.3,
#: renamed from the former ``loop-owner`` / ``GLOBAL_OWNER_SLOT``; #1073).
#: This is the DEFAULT owner slot ``t3 loop owner`` claims; its
#: pid-anchored, hijack-guarded semantics are unchanged. Per-loop owners
#: live in the ``loop:<name>`` namespace below — a disjoint key space, so a
#: per-loop claim can never collide with or evict the global owner.
T3_MASTER_SLOT = "t3-master"

#: Prefix for the additive per-loop owning-session layer (#1834). A
#: dedicated loop (PR#3) claims ``loop:<name>`` (e.g. ``loop:dispatch``)
#: so two dedicated loops can be owned by two different sessions
#: concurrently. The prefix keeps the per-loop keys in their own namespace,
#: disjoint from ``T3_MASTER_SLOT`` and from the infra-slot leases
#: (``loop-tick`` / ``loop-self-improve`` / …), which use ``-`` not ``:``.
PER_LOOP_OWNER_PREFIX = "loop:"


def per_loop_owner_slot(loop_name: str) -> str:
    """Canonical owner-slot key for a dedicated per-loop owning session (#1834).

    The fully-qualified form ``loop:<loop_name>`` is the canonical key —
    every per-loop claim/read/compare normalizes UP to it at the boundary
    so a bare ``dispatch`` and a qualified ``loop:dispatch`` can never be
    treated as two different slots. The global single-owner slot is the
    reserved :data:`T3_MASTER_SLOT` constant, never produced by this
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


#: Prefix for the transient PER-LOOP tick mutex (#1834/#2650). A single-loop
#: tick (``t3 loops tick --loop <name>``) acquires ``loop-tick:<name>`` for the
#: duration of its beat to serialise concurrent ticks of the SAME loop, then
#: releases it in a ``finally``. It is disjoint from the bare master
#: ``loop-tick`` mutex (no ``:``) and from the durable ``loop:<name>`` owner
#: lease: whenever a per-loop tick holds ``loop-tick:<name>`` it ALSO holds the
#: matching ``loop:<name>`` owner lease (claimed first), so the mutex is pure
#: implementation detail — a concurrency lock, not a user-facing loop. The
#: statusline never renders it; the loop is already represented by its
#: ``loop:<name>`` owner-lease chunk.
PER_LOOP_TICK_MUTEX_PREFIX = "loop-tick:"


def is_per_loop_tick_mutex(slot: str) -> bool:
    """Whether ``slot`` is a transient per-loop tick mutex (``loop-tick:<name>``).

    Distinct from the bare master ``loop-tick`` mutex (no trailing ``:``), which
    is NOT a per-loop mutex and is left untouched.
    """
    return slot.startswith(PER_LOOP_TICK_MUTEX_PREFIX)


class OwnershipStatus(NamedTuple):
    """Read-only snapshot of a session-scoped t3-master claim (#1073/#1604).

    ``is_live`` is the predicate callers branch on. It is pid-anchored
    (matching ``claim_ownership``'s liveness): ``True`` iff a non-empty
    session holds a claim that is either unexpired OR whose ``owner_pid``
    is still alive, keyed on ``session_id`` rather than ``owner``.

    ``generation`` is the current fencing / lease-generation token
    (autonomous-lane redesign §5) — the value a merge-worker dispatched now
    would stamp and later re-check at its git write. A missing row reports
    generation ``0``.
    """

    owner_session: str
    expires_at: datetime | None
    is_live: bool
    generation: int = 0
    driver: str = ""


class LoopLeaseQuerySet(models.QuerySet):
    def take_over_ownership(
        self,
        name: str,
        *,
        session_id: str,
        owner_pid: int | None = None,
        ttl_seconds: int = 1800,
        driver: str = "",
    ) -> tuple[bool, str]:
        """Unconditionally steal the ``name`` lease for ``session_id`` (the user hand-off).

        The ``t3 loop claim --take-over`` path: an unconditional UPDATE on
        ``name`` that evicts even a LIVE claimant, so the chat-only user can
        wrest the loop back from a hijacking session within one tick. This is
        the deliberate exception to :meth:`claim_ownership`'s pid-anchored CAS
        (which never evicts a live owner). A steal that installs a DIFFERENT
        holder bumps the fencing generation (§5) so the old holder's in-flight
        worker is fenced at its next git write, and installs the incoming
        ``driver`` verbatim; re-taking one's OWN claim keeps both.

        Returns ``(True, current_owner_session)`` — always wins; the owner is
        read back *after* the write.
        """
        now = timezone.now()
        expires = now + timedelta(seconds=ttl_seconds)
        self.get_or_create(name=name)
        prior = self.filter(name=name).values_list("session_id", flat=True).first() or ""
        self.filter(name=name).update(
            session_id=session_id,
            owner_pid=owner_pid,
            acquired_at=now,
            lease_expires_at=expires,
            generation=self._generation_after(holder_changed=prior != session_id),
            driver=self._driver_after(driver, holder_changed=prior != session_id),
        )
        current = self.filter(name=name).values_list("session_id", flat=True).first() or ""
        return True, current

    def claim_ownership(
        self,
        name: str,
        *,
        session_id: str,
        owner_pid: int | None = None,
        ttl_seconds: int = 1800,
        driver: str = "",
    ) -> tuple[bool, str]:
        """Claim/refresh a persistent session-scoped owner slot (#1073).

        The pid-anchored CAS / per-tick heartbeat path — for the unconditional
        user hand-off that evicts a live claimant, see :meth:`take_over_ownership`.
        Returns ``(won, current_owner_session)`` (the owner read back *after* the
        write, so a loser reports WHO holds it). Liveness is the slot-aware
        :func:`live_foreign_owner_session` verdict: a genuinely-live foreign owner
        BLOCKS the claim (no write), otherwise the backend-agnostic conditional
        UPDATE CAS reclaims an unclaimed / this-session / stale row (correct on the
        production SQLite backend where ``select_for_update`` is a silent no-op —
        the #786 B1 lesson). No ``renew()`` / background timer (#54): the per-tick
        re-claim IS the heartbeat.

        An anonymous caller (``session_id == ""``) never persists a row: it RUNS
        iff there is no live owner (#1107 pure-cron), never erasing a live owner's
        row. A same-process self-reclaim across a session-id rotation (#2835 —
        compaction rotates the id but not the process, so ``owner_pid`` is the
        caller's own) re-anchors the lease to the new id and WINS, so any slot
        self-heals without a manual ``t3 loop claim --take-over``.

        ``owner_pid`` (#1604) is the durable session process id (not the ephemeral
        tick subprocess); stored so :meth:`evict_stale_owner` can distinguish a
        same-process self-reclaim (same pid) from a live foreign session, and a
        null is treated conservatively as "unknown → KEEP" (INV4).
        """
        now = timezone.now()
        expires = now + timedelta(seconds=ttl_seconds)
        self.get_or_create(name=name)
        # Never anchor the lease on a provably-dead pid (#3646): a stale registry
        # pid would make the next reclaim sweep read this very claim as
        # dead-owned and evict it, re-entering the reclaim path every tick.
        owner_pid = anchorable_owner_pid(owner_pid)

        row = self.filter(name=name).values("session_id", "owner_pid", "lease_expires_at").first()
        live_owner = live_foreign_owner_session(
            row, session_id, now, trust_pid_past_ttl=not is_per_loop_owner_slot(name)
        )

        if not session_id:
            # An anonymous caller never persists ownership. It RUNS iff
            # there is no live owner (so pure-cron deployments still tick),
            # and never erases a live owner's row.
            return not live_owner, live_owner

        if live_owner:
            stored_pid = (row or {}).get("owner_pid")
            if not pid_is_foreign(stored_pid, owner_pid):
                # Same-process self-reclaim across a session-id rotation (#2835):
                # context compaction rotates ``session_id`` but does NOT restart
                # the durable session process, so ``owner_pid`` is unchanged — the
                # live lease is still ours. Re-anchor it to the rotated session id
                # and refresh the TTL so the slot self-heals on the next tick. The
                # CAS re-asserts the exact stored pid so a concurrent claim that
                # already moved the lease off it is never clobbered. The
                # generation is KEPT — a same-process rotation is not a transfer
                # (§5), so the master never fences its own worker across a compaction.
                won = self.filter(name=name, owner_pid=stored_pid).update(
                    session_id=session_id,
                    owner_pid=owner_pid,
                    acquired_at=now,
                    lease_expires_at=expires,
                    # A same-process rotation is not a transfer, so preserve the
                    # stored driver when detection comes back blank (edge-case 1).
                    driver=self._driver_after(driver, holder_changed=False),
                )
                current = self.filter(name=name).values_list("session_id", flat=True).first() or ""
                return won == 1, current
            # A genuinely foreign live owner (a DIFFERENT alive pid, or an
            # indeterminate null pid within its TTL) blocks the claim — no write.
            return False, live_owner

        prior_session = (row or {}).get("session_id") or ""
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
                # Reclaiming an unowned/expired slot from a DIFFERENT prior holder
                # is a holder change → bump the fencing generation (§5). A same-
                # session refresh keeps it (the per-tick heartbeat is not a transfer).
                generation=self._generation_after(holder_changed=prior_session != session_id),
                driver=self._driver_after(driver, holder_changed=prior_session != session_id),
            )
        )
        current = self.filter(name=name).values_list("session_id", flat=True).first() or ""
        return won == 1, current

    @staticmethod
    def _generation_after(*, holder_changed: bool) -> Combinable:
        """The ``generation=`` value for a winning claim write (§5 fencing token).

        A holder change increments the token; a same-holder refresh / same-process
        self-reclaim keeps it via an identity ``F("generation")``. The write always
        carries a ``generation=`` expression, and the ``F`` reference makes it
        atomic against the row's live value — so a concurrent refresh between the
        pre-read and this write cannot desync the counter.
        """
        return F("generation") + 1 if holder_changed else F("generation")

    @staticmethod
    def _driver_after(driver: str, *, holder_changed: bool) -> Combinable | str:
        """The ``driver=`` value for a winning claim write (PR-26 tick-driver token).

        A holder change installs the incoming ``driver`` VERBATIM (including ``""``):
        a new holder that registers no driver is genuinely driverless, so it must
        never inherit the dead owner's label. A same-holder refresh / same-process
        self-reclaim PRESERVES the stored value when ``driver`` is empty
        (``F("driver")``) — the per-tick heartbeat re-claims every tick, so a tick
        whose detection momentarily returns blank must not wipe the registration.
        Mirrors :meth:`_generation_after`'s F-expression idiom so the write stays
        atomic against the row's live value.
        """
        if holder_changed:
            return driver
        return driver or F("driver")

    def fencing_generation(self, name: str) -> int:
        """Current fencing / lease-generation token for ``name`` (§5).

        The value a merge-worker dispatched now would stamp. A missing row
        reports ``0`` — an unclaimed slot has never changed hands.
        """
        return self.filter(name=name).values_list("generation", flat=True).first() or 0

    def token_is_current(self, name: str, token: int) -> bool:
        """Whether ``token`` still matches the live fencing generation (§5).

        The git-write fencing check: a merge-worker's write is admitted only
        while no newer generation has been granted. A stale token (a higher
        generation was installed by a failover or a human steal after the worker
        was dispatched) is fenced out. Equality is exact because the generation
        only ever increases, so a worker can never legitimately hold a token
        above the current one.
        """
        return token == self.fencing_generation(name)

    def live_foreign_owner(self, name: str, *, session_id: str, current_pid: int | None) -> str:
        """The session of a genuinely LIVE foreign owner of ``name``, or ``""`` (#1604).

        The READ predicate the SessionStart/UserPromptSubmit desync check consults:
        a slot owned by a DIFFERENT, still-live session that is also a DIFFERENT OS
        process means that session is the rightful owner and a fresh session must
        stay idle (INV1). Reuses :func:`live_foreign_owner_session` for the
        foreign-and-live decision (the same pid-anchored liveness ``claim_ownership``
        and ``evict_stale_owner`` use) plus the :func:`pid_is_foreign` carve-out so
        a same-process self-reclaim is never reported as foreign. Returns ``""``
        when the slot is unowned, owned by ``session_id`` itself, owned by a
        dead/expired owner, or owned by this very process.
        """
        now = timezone.now()
        row = self.filter(name=name).values("session_id", "owner_pid", "lease_expires_at").first()
        owner = live_foreign_owner_session(row, session_id, now, trust_pid_past_ttl=not is_per_loop_owner_slot(name))
        if not owner:
            return ""
        return owner if pid_is_foreign((row or {}).get("owner_pid"), current_pid) else ""

    def evict_stale_owner(
        self,
        name: str,
        *,
        keep_session_id: str,
        current_pid: int | None,
    ) -> int:
        """Evict the ``name`` lease iff it is safe to do so (#1604/#1675).

        Decision table (INV1 / INV4 / #786 B1 backend-agnostic CAS). Liveness
        routes through the slot-aware :func:`lease_is_live`, so a busy
        ``t3-master`` owner is LIVE and never blanked (#1604) while a ``loop:
        <name>`` owner past its TTL is NOT live regardless of a reused alive pid
        and so is EVICTED (#3571):

        - Not live — a determinately-DEAD ``owner_pid`` (ANY TTL); an
            indeterminate pid past an EXPIRED TTL; or (per-loop) any owner past
            an EXPIRED TTL: EVICT (the owning session is gone).
        - Live + same pid: EVICT (post-compaction same-process self-reclaim —
            the pid match is the safety condition, regardless of TTL).
        - Live + null owner_pid: KEEP (unknown process, INV4 bias).
        - Live + alive owner_pid != current_pid: KEEP (INV1, foreign lease).

        The final UPDATE re-asserts the safety condition in its ``WHERE``
        clause (backend-agnostic CAS) so a concurrent tick that refreshed
        the lease between our read and this write is not evicted: a lapsed
        TTL is re-asserted as still-lapsed, and a determinately-dead
        ``owner_pid`` as still that exact pid (a concurrent claim moves
        ``owner_pid`` off it, so the CAS then matches nothing).

        Returns the number of rows orphaned (0 or 1).
        """
        pid_alive = pid_alive_probe()
        now = timezone.now()
        candidates = self.filter(name=name).exclude(session_id=keep_session_id)
        row = candidates.values("session_id", "owner_pid", "lease_expires_at").first()
        if not row or not (row["session_id"] or ""):
            return 0

        expires_at = row["lease_expires_at"]
        stored_pid = row["owner_pid"]
        is_live = lease_is_live(
            row["session_id"], stored_pid, expires_at, now, trust_pid_past_ttl=not is_per_loop_owner_slot(name)
        )

        if not is_live:
            # The owner is gone — either an expired TTL with an indeterminate
            # pid, or a determinately-dead ``owner_pid`` even within an
            # unexpired TTL. Re-assert the not-live condition in the CAS: a
            # still-lapsed TTL, OR (for the dead-pid-within-TTL case) the
            # exact dead pid we read — so a concurrent refresh/claim (which
            # extends the TTL and/or moves ``owner_pid``) is never clobbered.
            cas = Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
            if stored_pid is not None:
                cas |= Q(owner_pid=stored_pid)
            return candidates.filter(cas).update(session_id="", owner_pid=None, acquired_at=None, lease_expires_at=None)

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

    def reclaim_dead_owner_leases(self, *, current_pid: int | None = None) -> list[str]:
        """Orphan every owner-slot lease whose owning SESSION is provably dead (#3571).

        The background reclaim the worker supervisor, ``run_boot_sweeps`` (``t3
        recover``) and the self-heal watchdog run on a cadence so a dead session's
        lease is returned to the pool instead of blocking the live worker forever.
        Delegates the per-slot decision to :meth:`evict_stale_owner` (``keep_session_id
        =""`` keeps nothing; ``current_pid=None`` so the same-process carve-out never
        fires), which routes through the shared discriminator — a busy ``t3-master``
        owner is KEPT (#1604), a per-loop lease past its TTL is reclaimed regardless of
        a reused pid, a fresh-heartbeat owner is never touched. Idempotent and
        conservative. Returns the reclaimed slot names; every eviction is logged loudly.
        """
        reclaimed: list[str] = []
        owned = self.exclude(session_id="").values("name", "session_id", "owner_pid", "lease_expires_at")
        for row in list(owned):
            name = row["name"]
            if not self.evict_stale_owner(name, keep_session_id="", current_pid=current_pid):
                continue
            reclaimed.append(name)
            logger.warning(
                "Reclaimed dead-owner loop lease %r (session %r, owner_pid %s, expired at %s): the owning "
                "session is provably dead past TTL, returning the lease to the pool (#3571).",
                name,
                row["session_id"],
                row["owner_pid"],
                row["lease_expires_at"],
            )
        return reclaimed

    def heartbeat_ownership(self, name: str, *, session_id: str, ttl_seconds: int = 1800) -> bool:
        """Extend the t3-master lease IFF this session still holds it (#1073).

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
        """Read-only snapshot of the named t3-master claim (#1073/#1604).

        ``is_live`` is pid-anchored via :func:`lease_is_live`: it
        is ``True`` iff a non-empty session holds a claim that is either
        unexpired (``lease_expires_at > now``) OR whose ``owner_pid`` is
        still alive — so the snapshot does not go blind during the busy-
        owner-past-TTL window the #1604 fix targets. A missing row reports
        ``("", None, False)`` — unclaimed.
        """
        row = (
            self.filter(name=name).values("session_id", "lease_expires_at", "owner_pid", "generation", "driver").first()
        )
        if row is None:
            return OwnershipStatus(owner_session="", expires_at=None, is_live=False, generation=0, driver="")
        session = row["session_id"] or ""
        expires_at = row["lease_expires_at"]
        is_live = lease_is_live(
            session, row["owner_pid"], expires_at, timezone.now(), trust_pid_past_ttl=not is_per_loop_owner_slot(name)
        )
        return OwnershipStatus(
            owner_session=session,
            expires_at=expires_at,
            is_live=is_live,
            generation=row["generation"],
            driver=row["driver"] or "",
        )

    def release_ownership(self, name: str, *, session_id: str) -> bool:
        """Release the t3-master claim iff held by ``session_id`` (CAS).

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
                # Release = no owner = no driver, by definition.
                driver="",
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
