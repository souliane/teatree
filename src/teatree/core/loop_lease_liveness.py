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

A pid means something only in the pid NAMESPACE it was recorded in (#4253), so every
predicate here first asks :func:`_pid_is_attributable` whether the reader shares that
namespace. Each service in the deployment runs in its own, so the same integer names
a different process — or none at all — depending on who reads it, and both
misreadings were measured on one box: an absent number reported the live worker's own
lease as provably dead, so every owner-gated loop skipped while the worker drove
every tick; a number the reader's namespace happened to reuse read as alive and would
pin a dead owner's lease past its TTL forever. An unattributable pid is therefore no
evidence at all, and falls through to the TTL — the same degraded path a null pid
takes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

#: How long a PER-LOOP lease whose owner cannot be verified stays live.
#:
#: Three times the 60s per-tick re-claim heartbeat, so an owner that is actually
#: running never lapses even across two skipped ticks, while an owner that has
#: DIED releases its slots within minutes instead of pinning them for the whole
#: ``ttl_seconds`` (1800s) TTL. Without this bound an unverifiable owner received
#: MORE protection than a verifiable one — a provably-dead pid is reclaimable
#: immediately, so a null pid holding every ``loop:<name>`` slot for a full 30
#: minutes let a restarting worker be locked out by its own dead predecessor and
#: SKIP every loop indefinitely.
UNVERIFIABLE_OWNER_GRACE = timedelta(seconds=180)

#: The columns :meth:`LeaseClaim.from_row` reads, so a ``.values()`` call cannot
#: silently omit one and hand the predicates a blank field that reads as an absent
#: fact. Every claim-reading queryset selects exactly this tuple.
CLAIM_COLUMNS = ("session_id", "owner_pid", "owner_pid_namespace", "lease_expires_at", "acquired_at")


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    """The four stored facts about one lease claim that decide its liveness.

    They travel together through every predicate below because they are one
    lease, and reading them off a ``.values()`` dict at each call site is how
    they drift. :meth:`from_row` is the single place the ORM's column names are
    known, so a renamed column breaks in one place rather than four.
    """

    session_id: str
    owner_pid: int | None = None
    expires_at: datetime | None = None
    acquired_at: datetime | None = None
    owner_pid_namespace: str = ""

    @classmethod
    def from_row(cls, row: dict | None) -> "LeaseClaim":
        """Build a claim from a ``LoopLease`` ``.values()`` row; an absent row is an unowned slot."""
        values = row or {}
        return cls(
            session_id=values.get("session_id") or "",
            owner_pid=values.get("owner_pid"),
            expires_at=values.get("lease_expires_at"),
            acquired_at=values.get("acquired_at"),
            owner_pid_namespace=values.get("owner_pid_namespace") or "",
        )

    def within_ttl(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at > now

    def within_unverifiable_grace(self, now: datetime) -> bool:
        """Whether this claim is still inside :data:`UNVERIFIABLE_OWNER_GRACE`.

        The liveness bound for an owner whose process cannot be verified. Every
        winning claim write stamps ``acquired_at``, and the per-tick re-claim IS
        the heartbeat, so this reads as "did the owner prove liveness recently".
        A null ``acquired_at`` cannot date the claim at all and is therefore NOT
        within grace — an unverifiable owner biases to reclaimable, never pinned.
        """
        if self.acquired_at is None:
            return False
        return self.acquired_at + UNVERIFIABLE_OWNER_GRACE > now


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


def reader_pid_namespace() -> str:
    """This process's pid namespace — the frame every stored ``owner_pid`` is read in.

    One deferred-import seam every liveness call site shares, mirroring
    :func:`pid_alive_probe`, so a kernel without a readable procfs degrades identically
    everywhere: ``""`` is reported as unattributable rather than as a false match.
    """
    try:
        from teatree.utils.singleton import current_context  # noqa: PLC0415 — deferred: call-time import, kept lazy
    except ImportError:
        return ""
    return current_context().pid_namespace


def _pid_is_attributable(owner_pid_namespace: str, reader_namespace: str) -> bool:
    """Whether a pid recorded in ``owner_pid_namespace`` resolves in ``reader_namespace`` (#4253).

    A recorded namespace that DIFFERS from the reader's makes the pid no evidence: the
    probe would answer about whatever occupies that integer here, not about the owner.
    An UNRECORDED namespace on either side leaves the pid as the only evidence there
    is — every claim written since the column exists records one, so a blank decays on
    the owner's next heartbeat rather than needing a backfill.
    """
    if not owner_pid_namespace or not reader_namespace:
        return True
    return owner_pid_namespace == reader_namespace


def namespace_is_attributable(owner_pid_namespace: str) -> bool:
    """:func:`_pid_is_attributable` against THIS reader's namespace — the one call sites use.

    Every consumer resolves the reader namespace through this module rather than importing
    :func:`reader_pid_namespace` itself, so a test that pins one reader's namespace pins it
    for the whole decision instead of half of it. Takes the bare namespace rather than a
    :class:`LeaseClaim` so a caller holding a
    :class:`~teatree.core.models.loop_lease.LoopLease` row asks the same question.
    """
    return _pid_is_attributable(owner_pid_namespace, reader_pid_namespace())


def namespace_is_proven(owner_pid_namespace: str) -> bool:
    """Whether ``owner_pid_namespace`` is PROVED to be this reader's own (#4270).

    The strict sibling of :func:`namespace_is_attributable`, for a call site whose act on
    a "dead" verdict is DESTRUCTIVE rather than diagnostic. Blank tolerance is right where
    a wrong read costs a delayed reclaim under a TTL backstop; it is wrong where the read
    releases a live holder's lease and starts a second concurrent pass, because there is
    no backstop behind that release. An unrecorded namespace on either side is therefore
    missing evidence here, not a licence to fall back to the bare pid — which is why this
    is plain equality of two recorded namespaces rather than the blank-tolerant predicate.
    """
    reader = reader_pid_namespace()
    return bool(owner_pid_namespace) and bool(reader) and owner_pid_namespace == reader


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


def owner_pid_is_dead(owner_pid: int | None) -> bool:
    """Whether the pid probe POSITIVELY answers that ``owner_pid`` is gone.

    The one not-live reason that is PROOF, split out of :func:`reclaim_reason`'s
    prose so an eviction CAS can pick its predicate on it. A null pid and an
    unavailable probe are indeterminate, never proof.
    """
    if owner_pid is None:
        return False
    probe = pid_alive_probe()
    return probe is not None and not probe(owner_pid)


def lease_is_live(claim: LeaseClaim, now: datetime, *, trust_pid_past_ttl: bool) -> bool:
    """Whether a non-empty session's lease is live (#1073/#1604/#3571).

    The single liveness predicate every caller shares so they can never drift. A
    determinately-DEAD ``owner_pid`` is NOT live at ANY TTL. An ALIVE ``owner_pid``
    past an expired TTL depends on ``trust_pid_past_ttl`` — the #3571 crux, since a
    dead session's pid is routinely reused / cross-namespace so an alive pid is not
    proof the session is alive: ``True`` (``t3-master``) keeps it live past TTL (the
    #1604 busy-owner protection); ``False`` (a ``loop:<name>`` slot) falls through to
    the TTL so a lapsed TTL is reclaimable while a fresh TTL still reads live.
    An empty ``session_id`` is never live.

    An INDETERMINATE pid (null, ``pid_alive`` unavailable, or one
    :func:`_pid_is_attributable` says this reader cannot resolve) is the degraded case
    and gets the SHORTEST leash, not the longest. On a ``loop:<name>`` slot it is
    live only while BOTH the TTL holds AND the claim is inside
    :data:`UNVERIFIABLE_OWNER_GRACE`, because nothing else can distinguish a
    running owner from a dead one — and ``anchorable_owner_pid`` deliberately
    nulls a dead pid at claim time, so a session that dies leaves exactly this
    shape behind. ``t3-master`` keeps the plain TTL: its owner may be busy inside
    a long beat and fire no re-claim, so ``acquired_at`` is not a heartbeat there
    and the #1604 busy-owner protection must not be shortened.
    """
    if not claim.session_id:
        return False
    within_ttl = claim.within_ttl(now)
    if claim.owner_pid is not None and namespace_is_attributable(claim.owner_pid_namespace):
        pid_alive = pid_alive_probe()
        if pid_alive is not None:
            if not pid_alive(claim.owner_pid):
                return False
            if trust_pid_past_ttl:
                return True
            # Per-loop slot: an alive-but-possibly-reused pid does not extend
            # liveness past the TTL; fall through to the TTL backstop.
            return within_ttl
    if trust_pid_past_ttl:
        return within_ttl
    return within_ttl and claim.within_unverifiable_grace(now)


def reclaim_reason(owner_pid: int | None, *, owner_pid_namespace: str = "") -> str:
    """Why a reclaimed lease was not live, branched on the ACTUAL pid probe (#4141).

    :func:`lease_is_live` collapses disjoint not-live reasons into one boolean and
    only ONE of them is proof of death. A ``loop:<name>`` slot whose cadence is at
    least the lease TTL lapses between its own consecutive ticks, so its owner is
    routinely ALIVE when the sweep reclaims it; reporting proof there sends an
    operator diagnosing a real outage after a session that never died. A pid recorded
    in another namespace is the same hazard one step further out (#4253) — nothing
    here can say whether that process lives, so the reason says exactly that.
    """
    if owner_pid is None:
        return "the TTL lapsed without a re-claim and no owner pid was recorded"
    if not namespace_is_attributable(owner_pid_namespace):
        return (
            f"the TTL lapsed without a re-claim; owner pid {owner_pid} belongs to "
            f"{owner_pid_namespace}, which this process cannot resolve"
        )
    probe = pid_alive_probe()
    if probe is None:
        return f"the TTL lapsed without a re-claim; owner pid {owner_pid} could not be probed"
    if probe(owner_pid):
        return f"the TTL lapsed without a re-claim; owner pid {owner_pid} is still alive"
    return f"owner pid {owner_pid} is provably dead"


def live_foreign_owner_session(claim: LeaseClaim, session_id: str, now: datetime, *, trust_pid_past_ttl: bool) -> str:
    """The non-empty session of a live owner *other than* ``session_id``, or ``""``.

    Live is the slot-aware :func:`lease_is_live` verdict; the same session refreshing
    its own claim is never "foreign". Returns ``""`` when the slot is unowned, owned
    by ``session_id`` itself, or held by a dead/expired (reclaimable) owner.
    """
    if claim.session_id == session_id:
        return ""
    return claim.session_id if lease_is_live(claim, now, trust_pid_past_ttl=trust_pid_past_ttl) else ""


def pid_is_foreign(stored_pid: int | None, current_pid: int | None) -> bool:
    """Whether a live lease's ``owner_pid`` belongs to a DIFFERENT OS process (#1604).

    A live foreign-session lease whose ``owner_pid`` matches ``current_pid`` is a
    post-compaction same-process self-reclaim — the session rotated its id but the OS
    process is ours — so it is NOT a genuinely foreign owner. A null stored pid is
    treated as foreign (unknown → bias to report-foreign/KEEP).

    Compares bare integers, so callers holding a whole claim use
    :func:`claim_pid_is_foreign` instead — two processes in different pid namespaces
    routinely share a number, and reading that as one process is #4253.
    """
    return current_pid is None or stored_pid != current_pid


def claim_pid_is_foreign(claim: LeaseClaim, current_pid: int | None) -> bool:
    """Whether ``claim``'s owner is a DIFFERENT OS process than ``current_pid`` (#4253).

    The same-process carve-outs — ``claim_ownership``'s #2835 self-reclaim across a
    session-id rotation and ``evict_stale_owner``'s same-pid eviction — are keyed on an
    integer match, so a pid this reader cannot attribute must be foreign. Otherwise a
    container whose own pid happens to equal the worker's re-anchors or evicts the live
    worker's lease on nothing but a collision.
    """
    if not namespace_is_attributable(claim.owner_pid_namespace):
        return True
    return pid_is_foreign(claim.owner_pid, current_pid)
