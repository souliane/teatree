"""The reclaim warning must branch on the pid probe it already ran (#4141).

``lease_is_live`` returns not-live for two disjoint reasons — a pid the probe
answered DEAD, and (on a ``loop:<name>`` slot) a lapsed TTL under a still-ALIVE
pid — and the sweep's warning asserted the first unconditionally. Every loop
whose cadence is at least the lease TTL lapses between its own consecutive
ticks, so the false "provably dead" was permanent noise on exactly the loops an
operator investigating an outage reads first.
"""

import datetime as dt
import logging

import pytest
from django.utils import timezone

import teatree.utils.singleton as singleton_mod
from teatree.core.loop_lease_liveness import reclaim_reason
from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PER_LOOP_SLOT = "loop:issue_implementer"
_MASTER_SLOT = "t3-master"
_OWNER_SESSION = "loop-runner"
_OWNER_PID = 7


def _seed(slot: str, *, owner_pid: int | None) -> None:
    now = timezone.now()
    LoopLease.objects.create(
        name=slot,
        session_id=_OWNER_SESSION,
        owner_pid=owner_pid,
        acquired_at=now - dt.timedelta(seconds=3600),
        lease_expires_at=now - dt.timedelta(seconds=60),
    )


def _sweep_warning(caplog: pytest.LogCaptureFixture) -> str:
    with caplog.at_level(logging.WARNING, logger="teatree.core.loop_lease_manager"):
        reclaimed = LoopLease.objects.reclaim_dead_owner_leases()
    assert reclaimed, "the lease must have been reclaimed for a warning to exist"
    return "\n".join(rec.getMessage() for rec in caplog.records)


class TestReclaimWarningBranchesOnTheProbe:
    """The sweep's warning says what the probe actually answered."""

    def test_a_lapsed_ttl_under_a_live_pid_is_not_called_provably_dead(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)
        _seed(_PER_LOOP_SLOT, owner_pid=_OWNER_PID)

        message = _sweep_warning(caplog)

        assert "provably dead" not in message
        assert f"owner pid {_OWNER_PID} is still alive" in message

    def test_a_dead_pid_is_still_reported_as_proof(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: False)
        _seed(_MASTER_SLOT, owner_pid=_OWNER_PID)

        assert f"owner pid {_OWNER_PID} is provably dead" in _sweep_warning(caplog)

    def test_a_null_pid_is_not_called_provably_dead(self, caplog: pytest.LogCaptureFixture) -> None:
        _seed(_PER_LOOP_SLOT, owner_pid=None)

        message = _sweep_warning(caplog)

        assert "provably dead" not in message
        assert "no owner pid was recorded" in message

    def test_the_slot_and_session_stay_in_the_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        _seed(_PER_LOOP_SLOT, owner_pid=None)

        message = _sweep_warning(caplog)

        assert _PER_LOOP_SLOT in message
        assert _OWNER_SESSION in message


class TestReclaimReason:
    """The ORM-free note itself, over the three probe outcomes."""

    def test_an_alive_pid_names_the_lapsed_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)

        assert reclaim_reason(_OWNER_PID) == f"the TTL lapsed without a re-claim; owner pid {_OWNER_PID} is still alive"

    def test_a_dead_pid_is_the_only_proof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: False)

        assert reclaim_reason(_OWNER_PID) == f"owner pid {_OWNER_PID} is provably dead"

    def test_an_unprobeable_pid_claims_no_proof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(singleton_mod, "pid_alive")

        reason = reclaim_reason(_OWNER_PID)

        assert "provably dead" not in reason
        assert f"owner pid {_OWNER_PID} could not be probed" in reason

    def test_a_null_pid_claims_no_proof(self) -> None:
        assert reclaim_reason(None) == "the TTL lapsed without a re-claim and no owner pid was recorded"
