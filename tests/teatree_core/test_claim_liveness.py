"""#4164: a lapsed lease is evidence about the LEASE, not about the run still driving it."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

import teatree.utils.singleton as singleton_mod
from teatree.core import loop_lease_liveness as liveness
from teatree.core.claim_liveness import (
    ClaimOwner,
    current_owner,
    driving,
    executing_owner_reason,
    owner_is_executing,
    reset_driving_registry,
)
from teatree.core.loop_lease_liveness import reader_pid_namespace as real_reader_namespace

#: A pinned reader namespace, so "this process" and "a foreign namespace" are both
#: decidable whatever procfs the test host exposes.
_READER_NS = "pid:[4026531836]"
_OTHER_NS = "pid:[4026999999]"

_TASK = 77


@contextmanager
def pinned_reader_namespace() -> Iterator[None]:
    """Pin the namespace every liveness decision reads, at the one seam they share."""
    with patch.object(liveness, "reader_pid_namespace", return_value=_READER_NS):
        yield


@pytest.fixture(autouse=True)
def _pin_reader_namespace() -> Iterator[None]:
    with pinned_reader_namespace():
        yield


def this_process(**overrides: object) -> ClaimOwner:
    fields: dict[str, object] = {"owner_pid": os.getpid(), "owner_pid_namespace": _READER_NS}
    fields.update(overrides)
    return ClaimOwner(**fields)


class TestOwnerIsExecuting:
    def test_a_task_this_process_is_driving_is_executing(self) -> None:
        with driving(_TASK):
            assert owner_is_executing(this_process(), _TASK)

    def test_a_lapsed_lease_is_not_part_of_the_evidence(self) -> None:
        """The whole point: nothing about the lease enters this decision."""
        with driving(_TASK):
            assert owner_is_executing(this_process(), _TASK)

    def test_a_task_nothing_is_driving_is_not_executing(self) -> None:
        """A crashed job leaves the worker alive; its row must still reclaim normally."""
        assert not owner_is_executing(this_process(), _TASK)

    def test_the_registry_is_released_even_when_the_drive_raises(self) -> None:
        boom = RuntimeError("drive died")
        with pytest.raises(RuntimeError, match="drive died"), driving(_TASK):
            raise boom

        assert not owner_is_executing(this_process(), _TASK)

    def test_a_different_task_is_not_executing(self) -> None:
        with driving(_TASK):
            assert not owner_is_executing(this_process(), _TASK + 1)

    def test_an_owner_in_another_process_is_not_executing(self) -> None:
        """Nothing here can see whether that process is driving, so it gets the lease verdict."""
        with driving(_TASK):
            assert not owner_is_executing(this_process(owner_pid=os.getpid() + 1), _TASK)

    def test_no_recorded_owner_is_not_executing(self) -> None:
        with driving(_TASK):
            assert not owner_is_executing(this_process(owner_pid=None), _TASK)

    def test_a_matching_pid_from_another_namespace_is_not_executing(self) -> None:
        """#4253: an equal integer in a foreign namespace is a collision, not this process."""
        with driving(_TASK):
            assert not owner_is_executing(this_process(owner_pid_namespace=_OTHER_NS), _TASK)

    def test_the_registry_is_visible_across_threads(self) -> None:
        """The sweeps run on the ``loops`` executor thread; the drive runs on ``default``."""
        seen: list[bool] = []
        with driving(_TASK):
            probe = threading.Thread(target=lambda: seen.append(owner_is_executing(this_process(), _TASK)))
            probe.start()
            probe.join()

        assert seen == [True]


class TestClaimOwnerReading:
    def test_of_reads_the_owner_columns_off_a_row(self) -> None:
        class Row:
            owner_pid = 4321
            owner_pid_namespace = _READER_NS

        assert ClaimOwner.of(Row()) == ClaimOwner(4321, _READER_NS)

    def test_of_degrades_a_row_missing_the_columns_to_no_evidence(self) -> None:
        assert ClaimOwner.of(object()) == ClaimOwner(None, "")

    def test_current_owner_reports_this_process(self) -> None:
        """``current_owner`` binds the real seam at import, so the pin above does not reach it."""
        pid, namespace = current_owner()
        assert pid == os.getpid()
        assert namespace == real_reader_namespace()

    def test_reason_names_the_pid_that_withheld_the_reap(self) -> None:
        assert "4321" in executing_owner_reason(ClaimOwner(4321, _READER_NS))


class TestResettingTheRegistry:
    def test_reset_clears_an_entry_a_test_leaked(self) -> None:
        """The conftest autouse roster's efficacy: pk-keyed entries must not outlive a test."""
        with pinned_reader_namespace():
            owner = ClaimOwner(os.getpid(), _READER_NS)
            with driving(_TASK):
                assert owner_is_executing(owner, _TASK)
                reset_driving_registry()
                assert not owner_is_executing(owner, _TASK)


#: A pid this test process is not, so :func:`this_process` never accidentally
#: shadows it — the cross-process tier only ever fires for a DIFFERENT pid.
_OTHER_PID = 999999
_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def another_process(**overrides: object) -> ClaimOwner:
    fields: dict[str, object] = {
        "owner_pid": _OTHER_PID,
        "owner_pid_namespace": _READER_NS,
        "owner_driving_since": _SINCE,
    }
    fields.update(overrides)
    return ClaimOwner(**fields)


class TestOwnerIsExecutingCrossProcess:
    """#4164 follow-up: the cross-process tier, exercised without the registry.

    ``reclaim_orphaned_claims``/``reap_stale_claims`` run in the loops_tick SUBPROCESS
    in production, a different interpreter from the one that drives headless work — so
    the in-memory ``driving`` registry (``TestOwnerIsExecuting`` above) is always empty
    there. This is the twin tier: ``owner_driving_since`` cross-checked against OS-level
    pid liveness. ``pid_alive`` (imported freshly by ``pid_alive_probe`` on every call —
    the shared seam ``loop_lease_liveness`` also patches at its source) is the one
    external probe, so it is the only thing patched.
    """

    def test_a_provably_alive_owner_with_a_driving_marker_is_executing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _pid: True)
        assert owner_is_executing(another_process(), _TASK)

    def test_a_provably_dead_owner_is_not_executing_even_with_a_stale_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash that skips drive_claim's finally leaves a stuck marker.

        The pid check is what still lets a genuinely dead owner's row reclaim normally.
        """
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _pid: False)
        assert not owner_is_executing(another_process(), _TASK)

    def test_no_driving_marker_is_not_executing_regardless_of_pid_liveness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare alive pid alone is no evidence.

        Trivially true in a single-worker deployment whether or not the job it
        claimed still exists.
        """
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _pid: True)
        assert not owner_is_executing(another_process(owner_driving_since=None), _TASK)

    def test_an_unattributable_namespace_is_not_executing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#4253: a pid recorded in a foreign namespace is no evidence, marker or not."""
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _pid: True)
        assert not owner_is_executing(another_process(owner_pid_namespace=_OTHER_NS), _TASK)

    def test_no_recorded_pid_is_not_executing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _pid: True)
        assert not owner_is_executing(another_process(owner_pid=None), _TASK)

    def test_an_unavailable_probe_degrades_to_not_executing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No procfs / no probe importable: indeterminate must never widen the guard."""
        monkeypatch.delattr(singleton_mod, "pid_alive")
        assert not owner_is_executing(another_process(), _TASK)
