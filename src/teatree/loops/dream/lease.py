"""Dead-owner-aware acquisition of the dream pass's in-flight lease (#3993).

The ``dream-tick`` lease TTL is sized to the pass budget (35 minutes), so a pass
killed mid-run — SIGKILL, container stop — never reaches its ``finally`` release and
its lease stays held for the rest of that window. A plain CAS refusal then reads
"another dream pass is already running" and sends the reader looking for a process
that does not exist.

The owner token is ``pid-<n>``, so the pid IS the liveness anchor: a lease whose owner
pid is PROVABLY dead is released and re-acquired. An owner that cannot be proved dead —
alive, not a pid token, or no ``pid_alive`` probe available — keeps it, the fail-closed
posture every other liveness call site takes (:mod:`teatree.core.loop_lease_liveness`).
"""

from dataclasses import dataclass

_OWNER_PREFIX = "pid-"


@dataclass(frozen=True, slots=True)
class LeaseVerdict:
    """Whether this caller holds the dream lease, plus the line to print about it.

    ``message`` is empty only on an uncontested acquire; a reclaim and a refusal both
    carry the line that tells the operator which of the two happened.
    """

    acquired: bool
    message: str


def lease_owner(pid: int) -> str:
    """The dream lease's owner token for *pid*."""
    return f"{_OWNER_PREFIX}{pid}"


def owner_pid(owner: str) -> int | None:
    """The pid encoded in *owner*, or ``None`` when it is not a ``pid-<n>`` token."""
    if not owner.startswith(_OWNER_PREFIX):
        return None
    tail = owner.removeprefix(_OWNER_PREFIX)
    return int(tail) if tail.isdigit() else None


def owner_is_dead(owner: str) -> bool:
    """Whether *owner*'s process is PROVABLY gone — unknown liveness is never dead."""
    from teatree.core.loop_lease_liveness import pid_alive_probe  # noqa: PLC0415 — deferred: call-time import

    pid = owner_pid(owner)
    if pid is None:
        return False
    probe = pid_alive_probe()
    return probe is not None and not probe(pid)


def acquire(*, owner: str, lease_seconds: int) -> LeaseVerdict:
    """Acquire the ``dream-tick`` lease, reclaiming it from a provably-dead holder.

    The reclaim releases on the HOLDER's own token, so it is a compare-and-swap that
    cannot evict a holder who changed between the failed acquire and the release; the
    re-acquire can still lose to a third pass, which is reported rather than retried.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loops.dream.loop import DREAM_LEASE_NAME  # noqa: PLC0415 — deferred: keeps import light

    if LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=owner, lease_seconds=lease_seconds):
        return LeaseVerdict(acquired=True, message="")
    holder = LoopLease.objects.filter(name=DREAM_LEASE_NAME).values_list("owner", flat=True).first() or ""
    if not owner_is_dead(holder):
        return LeaseVerdict(
            acquired=False,
            message=f"SKIP  a LIVE dream pass ({holder or 'unknown owner'}) holds the {DREAM_LEASE_NAME} lease.",
        )
    LoopLease.objects.release(DREAM_LEASE_NAME, owner=holder)
    if LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=owner, lease_seconds=lease_seconds):
        return LeaseVerdict(acquired=True, message=f"      reclaimed the {DREAM_LEASE_NAME} lease from dead {holder}.")
    return LeaseVerdict(
        acquired=False,
        message=f"SKIP  another dream pass took the {DREAM_LEASE_NAME} lease during the reclaim.",
    )


__all__ = ["LeaseVerdict", "acquire", "lease_owner", "owner_is_dead", "owner_pid"]
