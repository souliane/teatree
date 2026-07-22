"""Session-scoped t3-master claim on ``LoopLease`` (#1073).

The #1073 hijack: ``loops_tick`` re-acquires the ``loop-tick`` mutex with a
fresh ``pid-<pid>`` every tick, so between ticks ``loop-tick`` rests
``owner=""`` and ANY session running ``t3 loop tick`` (a statusline, an
unrelated blog-post session) wins the unowned CAS and does full loop work
— drains the user's Slack DMs, dispatches reviewers, runs CLEARs. The fix
is a persistent session-scoped ``t3-master`` claim the owning session
refreshes every tick (that re-claim IS the heartbeat); a non-owner
``loops_tick`` SKIPs before any scanner/drain/dispatch.

Keystone: ``TestCrossSessionLoopHijackOnSqlite`` reproduces the hijack on
the file-backed prod SQLite backend (RED before the gate: BOTH sessions'
ticks do work; GREEN after: exactly one ran). Modeled on
``TestLoopLeaseConcurrencyOnSqlite`` + the ``_make_alias`` file-backed
harness in ``test_on_behalf_approval_concurrent.py``.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.db import connections
from django.test import TestCase
from django.utils import timezone

from teatree.core.managers import (
    PER_LOOP_OWNER_PREFIX,
    T3_MASTER_SLOT,
    OwnershipStatus,
    is_per_loop_owner_slot,
    per_loop_owner_slot,
)
from teatree.core.models import LoopLease
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


class TestClaimOwnership(TestCase):
    def test_unowned_row_is_claimable(self) -> None:
        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        assert won is True
        assert owner == "sess-A"
        assert LoopLease.objects.get(name="t3-master").session_id == "sess-A"

    def test_same_session_reclaim_extends_expiry(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A", ttl_seconds=1)
        first_expiry = LoopLease.objects.get(name="t3-master").lease_expires_at
        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="sess-A", ttl_seconds=1800)
        assert won is True
        assert owner == "sess-A"
        assert LoopLease.objects.get(name="t3-master").lease_expires_at > first_expiry

    def test_different_live_session_is_blocked(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="sess-B")
        assert won is False
        # The loser learns WHO actually holds it.
        assert owner == "sess-A"
        assert LoopLease.objects.get(name="t3-master").session_id == "sess-A"

    def test_expired_claim_is_reclaimable(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="dead-sess", ttl_seconds=1)
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="successor")
        assert won is True
        assert owner == "successor"

    def test_take_over_evicts_live_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="hijacker")
        won, owner = LoopLease.objects.take_over_ownership("t3-master", session_id="main-session")
        assert won is True
        assert owner == "main-session"
        assert LoopLease.objects.get(name="t3-master").session_id == "main-session"

    def test_name_parameterized_slots_are_independent(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        won, owner = LoopLease.objects.claim_ownership("loop-slack-answer-owner", session_id="sess-B")
        assert won is True
        assert owner == "sess-B"
        assert LoopLease.objects.get(name="t3-master").session_id == "sess-A"

    def test_anonymous_claim_on_unowned_slot_wins_without_writing_owner(self) -> None:
        """An anonymous tick on an unowned slot RUNS (won) but never persists.

        Pure-cron / no-session deployments (#1107) still run the tick
        (``won=True``), but the row must stay ``session_id=""`` with no
        future expiry so the phantom "owned by nobody but not expired" row
        can never form.
        """
        LoopLease.objects.get_or_create(name="t3-master")
        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="")
        assert won is True
        assert owner == ""
        row = LoopLease.objects.get(name="t3-master")
        assert row.session_id == ""
        assert row.lease_expires_at is None
        assert LoopLease.objects.ownership_status("t3-master").is_live is False

    def test_alive_foreign_pid_blocks_reclaim_past_ttl(self) -> None:
        """A live owner_pid protects a foreign claim past its TTL (pid-anchored)."""
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-A", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="newcomer")
        assert won is False
        assert owner == "owner-A"
        assert LoopLease.objects.get(name="t3-master").session_id == "owner-A"

    def test_dead_pid_expired_lease_is_reclaimable(self) -> None:
        """A dead owner_pid + expired TTL stays reclaimable (no over-block)."""
        LoopLease.objects.claim_ownership("t3-master", session_id="dead", ttl_seconds=1, owner_pid=999999)
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="successor")
        assert won is True
        assert owner == "successor"

    def test_anonymous_claim_skips_when_live_real_owner(self) -> None:
        """An anonymous tick with a live real owner present SKIPs, row untouched."""
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-A")
        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="")
        assert won is False
        assert owner == "owner-A"
        assert LoopLease.objects.get(name="t3-master").session_id == "owner-A"


class TestHeartbeatOwnership(TestCase):
    def test_heartbeat_extends_when_still_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A", ttl_seconds=1)
        before = LoopLease.objects.get(name="t3-master").lease_expires_at
        assert LoopLease.objects.heartbeat_ownership("t3-master", session_id="sess-A", ttl_seconds=1800) is True
        assert LoopLease.objects.get(name="t3-master").lease_expires_at > before

    def test_heartbeat_fails_when_no_longer_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        LoopLease.objects.take_over_ownership("t3-master", session_id="taker")
        assert LoopLease.objects.heartbeat_ownership("t3-master", session_id="sess-A") is False


class TestReleaseOwnership(TestCase):
    def test_release_only_by_holder(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        assert LoopLease.objects.release_ownership("t3-master", session_id="someone-else") is False
        assert LoopLease.objects.get(name="t3-master").session_id == "sess-A"
        assert LoopLease.objects.release_ownership("t3-master", session_id="sess-A") is True
        assert LoopLease.objects.get(name="t3-master").session_id == ""

    def test_release_of_unclaimed_is_noop(self) -> None:
        LoopLease.objects.get_or_create(name="t3-master")
        assert LoopLease.objects.release_ownership("t3-master", session_id="") is False


class TestOwnershipStatus(TestCase):
    def test_missing_row_is_unclaimed(self) -> None:
        status = LoopLease.objects.ownership_status("t3-master")
        assert status == OwnershipStatus(owner_session="", expires_at=None, is_live=False)

    def test_live_claim_reports_session_and_is_live(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        status = LoopLease.objects.ownership_status("t3-master")
        assert status.owner_session == "sess-A"
        assert status.is_live is True
        assert status.expires_at is not None

    def test_expired_claim_is_not_live(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A", ttl_seconds=1)
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        status = LoopLease.objects.ownership_status("t3-master")
        assert status.owner_session == "sess-A"
        assert status.is_live is False

    def test_empty_session_is_not_live(self) -> None:
        LoopLease.objects.get_or_create(
            name="t3-master",
            defaults={"lease_expires_at": timezone.now() + timedelta(seconds=999)},
        )
        assert LoopLease.objects.ownership_status("t3-master").is_live is False

    def test_alive_pid_owner_past_ttl_reports_live(self) -> None:
        """A non-empty session with an alive owner_pid is_live past its TTL (pid-anchored)."""
        LoopLease.objects.claim_ownership("t3-master", session_id="busy", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        assert LoopLease.objects.ownership_status("t3-master").is_live is True

    def test_dead_pid_expired_is_not_live(self) -> None:
        """A dead owner_pid + expired TTL is not live (no over-block of the snapshot)."""
        LoopLease.objects.claim_ownership("t3-master", session_id="dead", ttl_seconds=1, owner_pid=999999)
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        assert LoopLease.objects.ownership_status("t3-master").is_live is False


class TestSessionIdentity(TestCase):
    def test_reads_claude_session_id(self) -> None:
        from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

        with mock.patch.dict("os.environ", {"CLAUDE_SESSION_ID": "claude-1"}, clear=True):
            assert current_session_id() == "claude-1"

    def test_falls_back_to_t3_loop_session_id(self) -> None:
        from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

        with mock.patch.dict("os.environ", {"T3_LOOP_SESSION_ID": "t3-loop-1"}, clear=True):
            assert current_session_id() == "t3-loop-1"

    def test_empty_when_neither_set(self) -> None:
        """No env vars + isolated empty registry dir → ``""``.

        #1107 widened ``current_session_id()`` with a third, lowest-precedence
        loop-registry fallback; pointing at an empty tmp dir keeps this
        invariant clean across environments (no leak from a real
        ``~/.local/share/teatree/loop-registry.json`` on the dev box).
        """
        import tempfile  # noqa: PLC0415

        from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": td}, clear=True),
        ):
            assert current_session_id() == ""

    def test_claude_session_id_wins_over_t3(self) -> None:
        from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

        with mock.patch.dict(
            "os.environ",
            {"CLAUDE_SESSION_ID": "claude-1", "T3_LOOP_SESSION_ID": "t3-loop-1"},
            clear=True,
        ):
            assert current_session_id() == "claude-1"

    def test_outbound_claim_reexport_is_the_same_callable(self) -> None:
        from teatree.core.session_identity import current_session_id as core_impl  # noqa: PLC0415
        from teatree.loop.session_identity import current_session_id as loop_reexport  # noqa: PLC0415
        from teatree.outbound_claim import _resolve_agent_session_id  # noqa: PLC0415

        # core is the canonical home; both the loop re-export and the
        # outbound_claim backward-compat alias resolve to the same object.
        assert loop_reexport is core_impl
        assert _resolve_agent_session_id is core_impl


# ── Keystone: cross-session hijack on the file-backed prod SQLite backend ──


def _make_alias(tmp_path: Path) -> str:
    """Register a file-backed SQLite connection with the prod write-mode.

    Mirrors ``test_on_behalf_approval_concurrent._make_alias``: prod's
    ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` (``BEGIN IMMEDIATE`` +
    busy_timeout) so a concurrent second writer serializes the same way
    production does, and ``AUTOCOMMIT=True`` so the racing claims are bare
    autocommit writes — no ``transaction.atomic()`` wrapper that would
    make the two ticks serialize and mask the hijack.
    """
    alias = f"loopown_{uuid.uuid4().hex}"
    db_file = tmp_path / f"{alias}.sqlite3"
    connections.databases[alias] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(db_file),
        "OPTIONS": dict(SQLITE_WRITE_SERIALIZATION_OPTIONS),
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {},
    }
    with connections[alias].cursor() as cur:
        cur.execute(
            """
            CREATE TABLE teatree_loop_lease (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(128) NOT NULL UNIQUE,
                owner VARCHAR(255) NOT NULL,
                session_id VARCHAR(255) NOT NULL DEFAULT '',
                owner_pid INTEGER NULL,
                acquired_at DATETIME NULL,
                lease_expires_at DATETIME NULL,
                generation INTEGER UNSIGNED NOT NULL DEFAULT 0,
                driver VARCHAR(16) NOT NULL DEFAULT ''
            )
            """
        )
    connections[alias].close()
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB guard — this test owns its own file DB alias."""
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestCrossSessionLoopHijackOnSqlite:
    """The #1073 hijack, reproduced on the file-backed prod SQLite backend.

    A "tick" here is the loops_tick gate's decision distilled to its
    essence: a session is allowed to do loop work iff its
    ``claim_ownership("t3-master", session_id=…)`` returns ``won=True``.

    RED (pre-fix): the gate does not exist, so loops_tick keyed loop work
    on the per-tick ``acquire("loop-tick", owner="pid-<pid>")`` only —
    which BOTH a hijacking session and the main session win between ticks
    (the lease rests unowned), so BOTH ticks ``ran``. To reproduce that
    pre-fix shape here, drop the ``claim_ownership`` gate and let both
    sessions through ⇒ ``len([t for t in ticks if t.ran]) == 2`` and this
    assertion fails.

    GREEN (post-fix): the persistent ``t3-master`` claim means the main
    session wins its first claim and the hijacker's claim is blocked while
    it is live ⇒ exactly one tick ``ran``.
    """

    def test_anti_vacuous_backend_is_file_backed_sqlite(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            conn = connections[alias]
            assert conn.vendor == "sqlite"
            assert conn.features.has_select_for_update_skip_locked is False
        finally:
            _teardown_alias(alias)

    def test_only_one_session_runs_the_tick(self, tmp_path: Path) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        alias = _make_alias(tmp_path)
        db_file = connections.databases[alias]["NAME"]
        try:
            # Two real subprocesses race the gated tick against the same
            # file DB. Each prints "RAN" iff its claim_ownership won —
            # exactly the loops_tick gate's pre-scanner decision.
            script = (
                "import os, sys, django;"
                "os.environ.setdefault('DJANGO_SETTINGS_MODULE','teatree.settings');"
                "django.setup();"
                "from django.db import connections;"
                "from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS;"
                "alias='hijack';"
                "connections.databases[alias]={"
                "'ENGINE':'django.db.backends.sqlite3','NAME':sys.argv[1],"
                "'OPTIONS':dict(SQLITE_WRITE_SERIALIZATION_OPTIONS),"
                "'ATOMIC_REQUESTS':False,'AUTOCOMMIT':True,'CONN_MAX_AGE':0,"
                "'CONN_HEALTH_CHECKS':False,'TIME_ZONE':None,'TEST':{}};"
                "from teatree.core.models import LoopLease;"
                "won,_=LoopLease.objects.using(alias).claim_ownership("
                "'t3-master',session_id=sys.argv[2]);"
                "print('RAN' if won else 'SKIP')"
            )
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(db_file), f"sess-{i}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for i in range(2)
            ]
            outs = [p.communicate(timeout=60) for p in procs]
            ran = [o for (o, _e) in outs if o.strip() == "RAN"]
            skipped = [o for (o, _e) in outs if o.strip() == "SKIP"]
            errs = [e for (_o, e) in outs if e.strip()]
            assert not errs, f"subprocess error: {errs!r}"
            # GREEN: exactly one session's tick ran. Pre-fix (no gate):
            # both would run — RED.
            assert len(ran) == 1, f"loop hijack NOT closed on prod SQLite: ran={outs!r}"
            assert len(skipped) == 1, f"expected one SKIP, got {outs!r}"
            row = LoopLease.objects.using(alias).get(name="t3-master")
            assert row.session_id in {"sess-0", "sess-1"}
        finally:
            _teardown_alias(alias)


class TestEvictStaleOwner(TestCase):
    """``LoopLeaseQuerySet.evict_stale_owner`` decision table (#1604/#1675).

    Verifies INV1 (never evict a live foreign lease), INV4 (null pid →
    KEEP), the pid-anchored no-hijack invariant (alive pid past TTL →
    KEEP), and the safe eviction paths: truly-dead (expired TTL + null/
    dead pid), dead pid, same-process post-compaction.
    """

    def test_expired_lease_is_evicted(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="old", ttl_seconds=1)
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        evicted = LoopLease.objects.evict_stale_owner("t3-master", keep_session_id="new", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name="t3-master").session_id == ""

    def test_live_foreign_alive_pid_is_kept(self) -> None:
        """INV1: live + alive pid of a different process → KEEP."""
        # Use the current process's own pid: it is alive, and is different
        # from keep_session_id's hypothetical pid only via the session-id
        # check. We set current_pid to a value that does NOT match stored pid.
        LoopLease.objects.claim_ownership("t3-master", session_id="foreign", ttl_seconds=1800, owner_pid=os.getpid())

        evicted = LoopLease.objects.evict_stale_owner(
            "t3-master",
            keep_session_id="new",
            current_pid=os.getpid() + 1,  # different from stored pid → foreign
        )
        assert evicted == 0
        assert LoopLease.objects.get(name="t3-master").session_id == "foreign"

    def test_live_null_pid_is_kept(self) -> None:
        """INV4: null stored pid + live lease → KEEP (unknown → bias preserve)."""
        LoopLease.objects.claim_ownership("t3-master", session_id="foreign", ttl_seconds=1800, owner_pid=None)

        evicted = LoopLease.objects.evict_stale_owner("t3-master", keep_session_id="new", current_pid=None)
        assert evicted == 0
        assert LoopLease.objects.get(name="t3-master").session_id == "foreign"

    def test_live_same_pid_is_evicted(self) -> None:
        """Post-compaction self-reclaim: live + same pid → EVICT."""
        current_pid = os.getpid()
        LoopLease.objects.claim_ownership(
            "t3-master", session_id="old-rotated", ttl_seconds=1800, owner_pid=current_pid
        )

        evicted = LoopLease.objects.evict_stale_owner(
            "t3-master", keep_session_id="new-rotated", current_pid=current_pid
        )
        assert evicted == 1
        assert LoopLease.objects.get(name="t3-master").session_id == ""

    def test_live_dead_pid_is_evicted(self) -> None:
        """Live lease whose owner process is dead → EVICT."""
        LoopLease.objects.claim_ownership("t3-master", session_id="dead-owner", ttl_seconds=1800, owner_pid=999999)

        evicted = LoopLease.objects.evict_stale_owner("t3-master", keep_session_id="new", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name="t3-master").session_id == ""

    def test_busy_foreign_owner_past_ttl_with_alive_pid_is_not_evicted(self) -> None:
        """No-hijack invariant: lapsed TTL but ALIVE foreign pid → KEEP (pid-anchored).

        The recurrence root cause: an alive-but-busy owner fires no Stop
        self-pump, so its lease TTL-lapses while the process is still
        alive. ``evict_stale_owner`` must treat liveness as pid-anchored
        (consistent with ``claim_ownership`` / ``ownership_status``) — a
        TTL-only ``is_live`` blanked the row to ``session_id=""``, letting
        a fresh SessionStart see an unowned slot and steal the loop. Keep
        the busy owner's claim so the loop is never hijacked.
        """
        LoopLease.objects.claim_ownership("t3-master", session_id="busy", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        evicted = LoopLease.objects.evict_stale_owner(
            "t3-master", keep_session_id="newcomer", current_pid=os.getpid() + 1
        )
        assert evicted == 0
        assert LoopLease.objects.get(name="t3-master").session_id == "busy"

    def test_expired_lease_alive_pid_then_claim_does_not_hijack(self) -> None:
        """End-to-end no-hijack: evict pass + newcomer claim leaves the busy owner in place.

        Reproduces the full recurrence shape: a busy owner past its TTL
        (alive pid), a fresh session runs the SessionStart eviction pass
        and then attempts to claim. With the pid-anchored evict, the row
        is never blanked, so the newcomer's anonymous-or-named claim is
        blocked and the owner keeps the loop.
        """
        LoopLease.objects.claim_ownership("t3-master", session_id="busy", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        LoopLease.objects.evict_stale_owner("t3-master", keep_session_id="newcomer", current_pid=os.getpid() + 1)
        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="newcomer")

        assert won is False
        assert owner == "busy"
        assert LoopLease.objects.get(name="t3-master").session_id == "busy"


# ── Per-loop owning-session layer (#1834, agent-teams Track-A PR#2) ──


class TestPerLoopOwnerSlot:
    """The canonical ``loop:<name>`` key derivation for the additive per-loop layer.

    The per-loop owner keys occupy a namespace disjoint from the global
    ``t3-master`` slot and the infra-slot leases (``loop-tick`` etc., which
    use ``-`` not ``:``), so a per-loop claim can never collide with the
    machine-wide single-owner lease.
    """

    def test_bare_name_is_qualified_up(self) -> None:
        assert per_loop_owner_slot("dispatch") == "loop:dispatch"

    def test_already_qualified_is_idempotent(self) -> None:
        assert per_loop_owner_slot("loop:review") == "loop:review"

    def test_whitespace_is_stripped(self) -> None:
        assert per_loop_owner_slot("  ship  ") == "loop:ship"

    def test_per_loop_key_is_never_the_global_slot(self) -> None:
        # The reserved global slot is distinct from any per-loop key, so the
        # default single-owner path and the new layer can never share a row.
        assert per_loop_owner_slot("owner") != T3_MASTER_SLOT
        assert not per_loop_owner_slot("dispatch").startswith(T3_MASTER_SLOT + "-")

    def test_is_per_loop_owner_slot_predicate(self) -> None:
        assert is_per_loop_owner_slot("loop:dispatch") is True
        assert is_per_loop_owner_slot(T3_MASTER_SLOT) is False
        assert is_per_loop_owner_slot("loop-tick") is False

    def test_prefix_constant_matches_derivation(self) -> None:
        assert per_loop_owner_slot("x").startswith(PER_LOOP_OWNER_PREFIX)


class TestPerLoopOwnershipReusesGlobalMachinery(TestCase):
    """Per-loop claims reuse the SAME CAS + pid-anchor + TTL machinery (#1834).

    No parallel weaker path: the manager is name-parameterized, so a
    per-loop slot gets identical empty-owner, pid-liveness, and take-over
    guards as the global ``t3-master``.
    """

    def test_two_loops_owned_by_two_sessions_concurrently(self) -> None:
        """No false cross-loop hijack — disjoint loops, disjoint sessions."""
        dispatch = per_loop_owner_slot("dispatch")
        review = per_loop_owner_slot("review")
        won_d, owner_d = LoopLease.objects.claim_ownership(dispatch, session_id="sess-dispatch")
        won_r, owner_r = LoopLease.objects.claim_ownership(review, session_id="sess-review")
        assert (won_d, owner_d) == (True, "sess-dispatch")
        assert (won_r, owner_r) == (True, "sess-review")
        assert LoopLease.objects.get(name=dispatch).session_id == "sess-dispatch"
        assert LoopLease.objects.get(name=review).session_id == "sess-review"

    def test_per_loop_does_not_evict_global_owner(self) -> None:
        """Claiming a per-loop slot never touches the global single-owner row."""
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="global-sess")
        LoopLease.objects.claim_ownership(per_loop_owner_slot("dispatch"), session_id="dispatch-sess")
        assert LoopLease.objects.get(name=T3_MASTER_SLOT).session_id == "global-sess"

    def test_per_loop_foreign_live_session_is_blocked(self) -> None:
        slot = per_loop_owner_slot("dispatch")
        LoopLease.objects.claim_ownership(slot, session_id="sess-A")
        won, owner = LoopLease.objects.claim_ownership(slot, session_id="sess-B")
        assert won is False
        assert owner == "sess-A"

    def test_per_loop_owner_reclaim_after_expiry(self) -> None:
        slot = per_loop_owner_slot("dispatch")
        LoopLease.objects.claim_ownership(slot, session_id="dead-sess", ttl_seconds=1)
        row = LoopLease.objects.get(name=slot)
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        won, owner = LoopLease.objects.claim_ownership(slot, session_id="successor")
        assert won is True
        assert owner == "successor"

    def test_per_loop_alive_pid_past_ttl_is_reclaimable(self) -> None:
        """A per-loop lease past its TTL is reclaimed even with an alive pid (#3571).

        The per-tick re-claim IS the owning session's heartbeat, so a lapsed TTL means
        the session stopped driving this loop; the pid can be reused / cross-namespace,
        so it is not trusted past the TTL for a ``loop:<name>`` slot. A FRESH-TTL live
        owner is still protected — see ``test_per_loop_foreign_live_session_is_blocked``.
        """
        slot = per_loop_owner_slot("dispatch")
        LoopLease.objects.claim_ownership(slot, session_id="busy", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name=slot)
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        won, owner = LoopLease.objects.claim_ownership(slot, session_id="newcomer")
        assert won is True
        assert owner == "newcomer"

    def test_master_alive_pid_blocks_reclaim_past_ttl(self) -> None:
        """The global ``t3-master`` slot keeps its #1604 busy-owner-past-TTL protection."""
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="busy", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name=T3_MASTER_SLOT)
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        won, owner = LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="newcomer")
        assert won is False
        assert owner == "busy"

    def test_per_loop_anonymous_claim_does_not_write_owner(self) -> None:
        """Empty-owner guard holds per-loop: anonymous claim runs but never persists a session."""
        slot = per_loop_owner_slot("dispatch")
        LoopLease.objects.get_or_create(name=slot)
        won, owner = LoopLease.objects.claim_ownership(slot, session_id="")
        assert won is True
        assert owner == ""
        assert LoopLease.objects.get(name=slot).session_id == ""

    def test_per_loop_dead_pid_evicted(self) -> None:
        """evict_stale_owner decision table applies per-loop (dead pid → EVICT)."""
        slot = per_loop_owner_slot("dispatch")
        LoopLease.objects.claim_ownership(slot, session_id="dead-owner", ttl_seconds=1800, owner_pid=999999)
        evicted = LoopLease.objects.evict_stale_owner(slot, keep_session_id="new", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name=slot).session_id == ""

    def test_per_loop_take_over_evicts_live_owner(self) -> None:
        slot = per_loop_owner_slot("dispatch")
        LoopLease.objects.claim_ownership(slot, session_id="hijacker")
        won, owner = LoopLease.objects.take_over_ownership(slot, session_id="main")
        assert won is True
        assert owner == "main"


class TestPerLoopClaimThroughManagementCommand(TestCase):
    """The existing ``loop_owner --slot`` CLI surface claims a per-loop slot (#1834).

    The dedicated-loop slot generator (PR#3) drives this exact path; here we
    prove a ``loop:<name>`` slot is claimable through the management command
    without touching the global ``t3-master`` row.
    """

    def test_claim_per_loop_slot_via_command(self) -> None:
        import io  # noqa: PLC0415

        from django.core.management import call_command  # noqa: PLC0415

        slot = per_loop_owner_slot("dispatch")
        with (
            mock.patch("teatree.loop.session_identity.current_session_id", return_value="sess-dispatch"),
            mock.patch("teatree.loop.driver_detection.detect_driver", return_value=""),
        ):
            out = io.StringIO()
            call_command("loop_owner", "claim", slot=slot, json_output=True, stdout=out)
        import json as _json  # noqa: PLC0415

        payload = _json.loads(out.getvalue())
        assert payload == {
            "ok": True,
            "slot": "loop:dispatch",
            "owner_session": "sess-dispatch",
            "driver": "",
            "driverless": True,
        }
        assert LoopLease.objects.get(name=slot).session_id == "sess-dispatch"
        assert not LoopLease.objects.filter(name="t3-master", session_id="sess-dispatch").exists()

    def test_per_loop_claim_is_pid_anchored(self) -> None:
        """A per-loop owner records owner_pid (hijack guard), not the None weaker path."""
        import io  # noqa: PLC0415

        from django.core.management import call_command  # noqa: PLC0415

        slot = per_loop_owner_slot("dispatch")
        with (
            mock.patch("teatree.loop.session_identity.current_session_id", return_value="sess-dispatch"),
            mock.patch("teatree.loop.session_identity.current_session_pid", return_value=os.getpid()),
        ):
            call_command("loop_owner", "claim", slot=slot, stdout=io.StringIO())
        assert LoopLease.objects.get(name=slot).owner_pid == os.getpid()
