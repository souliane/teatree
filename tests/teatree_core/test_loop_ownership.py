"""Session-scoped loop-owner claim on ``LoopLease`` (#1073).

The #1073 hijack: ``loop_tick`` re-acquires the ``loop-tick`` mutex with a
fresh ``pid-<pid>`` every tick, so between ticks ``loop-tick`` rests
``owner=""`` and ANY session running ``t3 loop tick`` (a statusline, an
unrelated blog-post session) wins the unowned CAS and does full loop work
— drains the user's Slack DMs, dispatches reviewers, runs CLEARs. The fix
is a persistent session-scoped ``loop-owner`` claim the owning session
refreshes every tick (that re-claim IS the heartbeat); a non-owner
``loop_tick`` SKIPs before any scanner/drain/dispatch.

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

from teatree.core.managers import OwnershipStatus
from teatree.core.models import LoopLease
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


class TestClaimOwnership(TestCase):
    def test_unowned_row_is_claimable(self) -> None:
        won, owner = LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A")
        assert won is True
        assert owner == "sess-A"
        assert LoopLease.objects.get(name="loop-owner").session_id == "sess-A"

    def test_same_session_reclaim_extends_expiry(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A", ttl_seconds=1)
        first_expiry = LoopLease.objects.get(name="loop-owner").lease_expires_at
        won, owner = LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A", ttl_seconds=1800)
        assert won is True
        assert owner == "sess-A"
        assert LoopLease.objects.get(name="loop-owner").lease_expires_at > first_expiry

    def test_different_live_session_is_blocked(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A")
        won, owner = LoopLease.objects.claim_ownership("loop-owner", session_id="sess-B")
        assert won is False
        # The loser learns WHO actually holds it.
        assert owner == "sess-A"
        assert LoopLease.objects.get(name="loop-owner").session_id == "sess-A"

    def test_expired_claim_is_reclaimable(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="dead-sess", ttl_seconds=1)
        row = LoopLease.objects.get(name="loop-owner")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        won, owner = LoopLease.objects.claim_ownership("loop-owner", session_id="successor")
        assert won is True
        assert owner == "successor"

    def test_take_over_evicts_live_owner(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="hijacker")
        won, owner = LoopLease.objects.claim_ownership("loop-owner", session_id="main-session", take_over=True)
        assert won is True
        assert owner == "main-session"
        assert LoopLease.objects.get(name="loop-owner").session_id == "main-session"

    def test_name_parameterized_slots_are_independent(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A")
        won, owner = LoopLease.objects.claim_ownership("loop-slack-answer-owner", session_id="sess-B")
        assert won is True
        assert owner == "sess-B"
        assert LoopLease.objects.get(name="loop-owner").session_id == "sess-A"


class TestHeartbeatOwnership(TestCase):
    def test_heartbeat_extends_when_still_owner(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A", ttl_seconds=1)
        before = LoopLease.objects.get(name="loop-owner").lease_expires_at
        assert LoopLease.objects.heartbeat_ownership("loop-owner", session_id="sess-A", ttl_seconds=1800) is True
        assert LoopLease.objects.get(name="loop-owner").lease_expires_at > before

    def test_heartbeat_fails_when_no_longer_owner(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A")
        LoopLease.objects.claim_ownership("loop-owner", session_id="taker", take_over=True)
        assert LoopLease.objects.heartbeat_ownership("loop-owner", session_id="sess-A") is False


class TestReleaseOwnership(TestCase):
    def test_release_only_by_holder(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A")
        assert LoopLease.objects.release_ownership("loop-owner", session_id="someone-else") is False
        assert LoopLease.objects.get(name="loop-owner").session_id == "sess-A"
        assert LoopLease.objects.release_ownership("loop-owner", session_id="sess-A") is True
        assert LoopLease.objects.get(name="loop-owner").session_id == ""

    def test_release_of_unclaimed_is_noop(self) -> None:
        LoopLease.objects.get_or_create(name="loop-owner")
        assert LoopLease.objects.release_ownership("loop-owner", session_id="") is False


class TestOwnershipStatus(TestCase):
    def test_missing_row_is_unclaimed(self) -> None:
        status = LoopLease.objects.ownership_status("loop-owner")
        assert status == OwnershipStatus(owner_session="", expires_at=None, is_live=False)

    def test_live_claim_reports_session_and_is_live(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A")
        status = LoopLease.objects.ownership_status("loop-owner")
        assert status.owner_session == "sess-A"
        assert status.is_live is True
        assert status.expires_at is not None

    def test_expired_claim_is_not_live(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="sess-A", ttl_seconds=1)
        row = LoopLease.objects.get(name="loop-owner")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])
        status = LoopLease.objects.ownership_status("loop-owner")
        assert status.owner_session == "sess-A"
        assert status.is_live is False

    def test_empty_session_is_not_live(self) -> None:
        LoopLease.objects.get_or_create(
            name="loop-owner",
            defaults={"lease_expires_at": timezone.now() + timedelta(seconds=999)},
        )
        assert LoopLease.objects.ownership_status("loop-owner").is_live is False


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
                lease_expires_at DATETIME NULL
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

    A "tick" here is the loop_tick gate's decision distilled to its
    essence: a session is allowed to do loop work iff its
    ``claim_ownership("loop-owner", session_id=…)`` returns ``won=True``.

    RED (pre-fix): the gate does not exist, so loop_tick keyed loop work
    on the per-tick ``acquire("loop-tick", owner="pid-<pid>")`` only —
    which BOTH a hijacking session and the main session win between ticks
    (the lease rests unowned), so BOTH ticks ``ran``. To reproduce that
    pre-fix shape here, drop the ``claim_ownership`` gate and let both
    sessions through ⇒ ``len([t for t in ticks if t.ran]) == 2`` and this
    assertion fails.

    GREEN (post-fix): the persistent ``loop-owner`` claim means the main
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
            # exactly the loop_tick gate's pre-scanner decision.
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
                "'loop-owner',session_id=sys.argv[2]);"
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
            row = LoopLease.objects.using(alias).get(name="loop-owner")
            assert row.session_id in {"sess-0", "sess-1"}
        finally:
            _teardown_alias(alias)


class TestEvictStaleOwner(TestCase):
    """``LoopLeaseQuerySet.evict_stale_owner`` decision table (#1604).

    Verifies INV1 (never evict a live foreign lease), INV4 (null pid →
    KEEP), and the safe eviction paths: expired, dead pid, same-process
    post-compaction.
    """

    def test_expired_lease_is_evicted(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="old", ttl_seconds=1)
        row = LoopLease.objects.get(name="loop-owner")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        evicted = LoopLease.objects.evict_stale_owner("loop-owner", keep_session_id="new", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name="loop-owner").session_id == ""

    def test_live_foreign_alive_pid_is_kept(self) -> None:
        """INV1: live + alive pid of a different process → KEEP."""
        # Use the current process's own pid: it is alive, and is different
        # from keep_session_id's hypothetical pid only via the session-id
        # check. We set current_pid to a value that does NOT match stored pid.
        LoopLease.objects.claim_ownership("loop-owner", session_id="foreign", ttl_seconds=1800, owner_pid=os.getpid())

        evicted = LoopLease.objects.evict_stale_owner(
            "loop-owner",
            keep_session_id="new",
            current_pid=os.getpid() + 1,  # different from stored pid → foreign
        )
        assert evicted == 0
        assert LoopLease.objects.get(name="loop-owner").session_id == "foreign"

    def test_live_null_pid_is_kept(self) -> None:
        """INV4: null stored pid + live lease → KEEP (unknown → bias preserve)."""
        LoopLease.objects.claim_ownership("loop-owner", session_id="foreign", ttl_seconds=1800, owner_pid=None)

        evicted = LoopLease.objects.evict_stale_owner("loop-owner", keep_session_id="new", current_pid=None)
        assert evicted == 0
        assert LoopLease.objects.get(name="loop-owner").session_id == "foreign"

    def test_live_same_pid_is_evicted(self) -> None:
        """Post-compaction self-reclaim: live + same pid → EVICT."""
        current_pid = os.getpid()
        LoopLease.objects.claim_ownership(
            "loop-owner", session_id="old-rotated", ttl_seconds=1800, owner_pid=current_pid
        )

        evicted = LoopLease.objects.evict_stale_owner(
            "loop-owner", keep_session_id="new-rotated", current_pid=current_pid
        )
        assert evicted == 1
        assert LoopLease.objects.get(name="loop-owner").session_id == ""

    def test_live_dead_pid_is_evicted(self) -> None:
        """Live lease whose owner process is dead → EVICT."""
        LoopLease.objects.claim_ownership("loop-owner", session_id="dead-owner", ttl_seconds=1800, owner_pid=999999)

        evicted = LoopLease.objects.evict_stale_owner("loop-owner", keep_session_id="new", current_pid=None)
        assert evicted == 1
        assert LoopLease.objects.get(name="loop-owner").session_id == ""
