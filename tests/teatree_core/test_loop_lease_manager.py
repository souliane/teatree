"""``LoopLeaseQuerySet.live_foreign_owner`` — the shared live-foreign-owner READ (#2777 C2).

The hook ``_db_live_foreign_owner`` reimplemented the foreign-and-live liveness
inline; this pins the manager's single predicate (which the hook now delegates
to) against the table of cases the inline version covered, and pins that the hook
delegates to it.
"""

import datetime as dt
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from django.utils import timezone

import hooks.scripts.hook_router as router
from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLOT = "loop-owner"


@dataclass(frozen=True)
class _Case:
    label: str
    row_session: str
    owner_pid: int | None
    expires_delta_seconds: int | None  # vs now; negative = expired; None = null expiry
    query_session: str
    current_pid: int | None
    alive_pids: frozenset[int]
    expected: str


_CASES = (
    _Case("unowned", "", None, 300, "me", 100, frozenset(), ""),
    _Case("own_session_refresh", "me", 100, 300, "me", 100, frozenset({100}), ""),
    _Case("live_foreign_diff_pid", "other", 4242, 300, "me", 100, frozenset({4242, 100}), "other"),
    _Case("expired_dead_pid", "other", 4242, -300, "me", 100, frozenset(), ""),
    _Case("same_pid_self_reclaim", "other", 100, 300, "me", 100, frozenset({100}), ""),
    _Case("null_pid_unexpired", "other", None, 300, "me", 100, frozenset(), "other"),
)


class TestLiveForeignOwner:
    @pytest.mark.parametrize("case", _CASES, ids=[c.label for c in _CASES])
    def test_equivalence_table(self, case: _Case) -> None:
        now = timezone.now()
        delta = case.expires_delta_seconds
        expires = None if delta is None else now + dt.timedelta(seconds=delta)
        LoopLease.objects.create(
            name=_SLOT, session_id=case.row_session, owner_pid=case.owner_pid, acquired_at=now, lease_expires_at=expires
        )
        with patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid in case.alive_pids):
            result = LoopLease.objects.live_foreign_owner(
                _SLOT, session_id=case.query_session, current_pid=case.current_pid
            )
        assert result == case.expected, case.label

    def test_missing_row_is_empty(self) -> None:
        assert LoopLease.objects.live_foreign_owner(_SLOT, session_id="me", current_pid=100) == ""


class TestHookDelegatesToManager:
    """``hook_router._db_live_foreign_owner`` is the disabled/bootstrap/fail-open envelope only.

    The foreign-and-live decision lives in the manager (mirroring the sibling
    ``_evict_stale_db_lease_owner`` which already routes through
    ``evict_stale_owner``). The hook must DELEGATE with the canonical slot + args.
    """

    def test_delegates_with_loop_owner_slot_and_args(self) -> None:
        with (
            patch.object(router, "_db_lease_consult_disabled", return_value=False),
            patch.object(router, "bootstrap_teatree_django", return_value=True),
            patch.object(LoopLease.objects, "live_foreign_owner", return_value="owner-x") as manager_call,
        ):
            result = router._db_live_foreign_owner("my-session", current_pid=4242)

        assert result == "owner-x"
        manager_call.assert_called_once_with("loop-owner", session_id="my-session", current_pid=4242)

    def test_envelope_fails_open_on_manager_error(self) -> None:
        with (
            patch.object(router, "_db_lease_consult_disabled", return_value=False),
            patch.object(router, "bootstrap_teatree_django", return_value=True),
            patch.object(LoopLease.objects, "live_foreign_owner", side_effect=RuntimeError("db hiccup")),
        ):
            assert router._db_live_foreign_owner("my-session", current_pid=4242) == ""
