"""DB lease for machine-wide loop ownership (#786 WS2; trimmed in #54).

Replaces the ``utils.singleton`` flock/pidfile t3-master guard. A flock
dies with its process and is invisible to any other process; a DB lease
row is queryable and reapable by expiry. Compaction-survival is NOT a
"renew" mechanism: each tick performs a fresh acquire (and the tick-owner
releases on a clean exit), so a stale lease simply expires and the next
tick's CAS reclaims it — there is no long-lived in-memory lease to renew.
(#54 removed the dead ``renew()`` method and the write-never-read
``heartbeat_at`` column the WS2 docstring oversold as a "heartbeat".)

Acquisition is a backend-agnostic atomic compare-and-swap — a single
conditional ``UPDATE ... WHERE (unowned OR lease expired)`` — NOT
``select_for_update(skip_locked=True)``: teatree's production DB is
SQLite, where ``has_select_for_update_skip_locked`` is ``False`` and that
clause is silently dropped (the #786 B1 lesson). Exactly one of N
concurrent ticks wins the CAS; the losers see 0 rows updated and skip.

#1073 deliberate exception — the ``t3-master`` row is PERSISTENT, not
per-tick. The "each tick performs a fresh acquire … a stale lease simply
expires" doctrine above is exact for the ``loop-tick`` concurrency mutex,
but it is precisely why a pid-keyed identity hijacks: between two ticks
``loop-tick`` rests ``owner=""``, so ANY session running ``t3 loop tick``
wins the unowned CAS and does loop work (drains the user's DMs, dispatches
reviewers). The fix is a second well-known row, ``t3-master``, holding a
session-scoped claim (``session_id`` column) that the owning session
*refreshes every tick* via ``claim_ownership`` — that per-tick re-claim IS
its heartbeat, so it is still expiry-reapable (a dead owner's claim lapses
after the TTL and the next session reclaims it) with NO renew() method and
NO background timer (#54 doctrine preserved). ``t3-master`` is never
released in the tick ``finally`` (only ``loop-tick`` is); the TTL is the
sole release. The CAS shape is name-parameterized so the in-flight
reactive-Slack-answer loop (#1063/#1069) reuses it via
``loop-slack-answer-owner``.

#1604: ``owner_pid`` makes the DB lease self-describing for eviction.
``evict_stale_owner`` uses it to distinguish a LIVE foreign claim (null or
different live pid → KEEP) from a post-compaction same-session restart
(same pid → safe to evict). A null ``owner_pid`` is treated conservatively
as "owner process unknown → KEEP" (INV4: bias toward preservation).

The GLOBAL ``t3-master`` slot's liveness is PID-ANCHORED, not TTL-anchored.
An owner that is alive but BUSY past the tick TTL fires no Stop self-pump, so
no tick re-claims and the lease TTL-lapses while the owner process is still
alive. ``claim_ownership`` therefore treats a non-empty ``t3-master`` owner
whose ``owner_pid`` is alive as a LIVE owner — protected past its TTL against
any non-``take_over`` claim from a DIFFERENT process — so the loop stays with
the existing process and transfers ONLY on that process's termination or an
explicit ``t3 loop claim --take-over``. A same-process session-id rotation
(#2835: context compaction rotates the id but not the process) is NOT a
transfer: the lease is re-anchored to the new session id and the same
process keeps the loop. The TTL is the FALLBACK release, used only
when ``owner_pid`` is null or dead. A ``loop:<name>`` PER-LOOP slot does NOT
trust ``pid_alive`` past its TTL (#3571): a dead session's pid is routinely
reused / cross-namespace, so once its TTL lapses the lease is reclaimable
regardless of pid liveness (the per-tick re-claim is that session's
heartbeat), while a fresh TTL still reads live. An anonymous caller (``session_id ==
""``, e.g. a Bash-tool tick that never sees the id, #1107) never persists
ownership: it runs the tick when unowned but can never write the phantom
"owned by nobody but not expired" row that previously enabled a fresh
session to hijack the loop.
"""

from django.db import models
from django.utils import timezone

from teatree.core.managers import LoopLeaseManager


class LoopDriver(models.TextChoices):
    """What mechanism fires ticks for an owned loop slot (PR-26 / M9).

    Ownership (a live ``session_id`` on the row) says WHO may run a loop;
    the driver says WHAT actually fires its ticks. A claim that registers
    under NONE of these is a stalled loop that still looks healthy — the
    ``LoopLease.driver`` invariant makes that state observable (a blank
    driver on an owned slot is DRIVERLESS, warned about loudly at claim
    time and on the statusline).

    Substrate-agnostic: detection reads the LIVE ``loop_runner_enabled``
    setting and the LIVE worker flock, so the same code is correct before
    and after the loop-runner default flip — only the observed distribution
    of values changes. ``EXTERNAL`` is never auto-detected (a foreign
    scheduler is invisible to teatree); it is set only via an explicit
    ``--driver external`` override.
    """

    SELF_PUMP = "self_pump"
    LOOP_RUNNER = "loop_runner"
    EXTERNAL = "external"


class LoopLease(models.Model):
    """One row per named machine-wide loop (e.g. ``loop-tick``)."""

    name = models.CharField(max_length=128, unique=True)
    owner = models.CharField(max_length=255, blank=True)
    session_id = models.CharField(max_length=255, blank=True, default="")
    owner_pid = models.IntegerField(null=True, blank=True, default=None)
    acquired_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    # The tick driver for this owned slot (PR-26 / M9). Blank = DRIVERLESS: an
    # owned slot with no recorded driver looks healthy but never ticks, so the
    # blank is surfaced loudly. Detected at claim time and self-healed on every
    # heartbeat re-claim; a same-holder refresh whose detection momentarily
    # fails PRESERVES the stored value (``_driver_after``) so the heartbeat can
    # never silently wipe the registration.
    driver = models.CharField(max_length=16, blank=True, default="", choices=LoopDriver)
    # Fencing / lease-generation token (autonomous-lane redesign §5). A
    # monotonically increasing counter bumped on every CHANGE of holder
    # (failover after expiry, or a human take-over steal); a same-holder
    # per-tick refresh and a same-process self-reclaim across a compaction
    # session-id rotation (#2835) both KEEP it, so the master never fences its
    # own in-flight worker. A merge-worker stamps the generation it was
    # dispatched under; a git write carrying a stale generation is fenced out,
    # closing the split-brain window a TTL lease alone leaves open.
    generation = models.PositiveIntegerField(default=0)

    objects = LoopLeaseManager()

    class Meta:
        db_table = "teatree_loop_lease"

    def __str__(self) -> str:
        return f"loop-lease<{self.name} owner={self.owner or '-'}>"

    @property
    def is_held(self) -> bool:
        """True iff the lease is owned and not yet expired."""
        if not self.owner or self.lease_expires_at is None:
            return False
        return self.lease_expires_at > timezone.now()
