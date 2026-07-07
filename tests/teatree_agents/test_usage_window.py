"""``teatree.agents.usage_window`` — park-not-fail + admission guard (Directive #3).

The DARK ``limit_autorecovery_enabled`` flag decides everything: OFF is byte-identical to
today (a limit is a terminal FAILED, no window row), ON parks the task and records the
window so the recovery chain can re-arm it at reset.
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

import django.test
from django.utils import timezone

import teatree.agents.usage_window as usage_window_mod
from teatree.agents.usage_window import effective_resets_at, maybe_park_for_active_window, park_task_on_limit
from teatree.core.models import LIMIT_PARKED_PREFIX, Session, Task, TaskAttempt, Ticket, UsageWindowState
from teatree.core.models.config_setting import ConfigSetting
from teatree.llm.anthropic_limits import LimitCause, LimitMatch


def _set_autorecovery(*, on: bool) -> None:
    ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=on)


class _DownError(RuntimeError):
    """A stand-in config-read failure the fail-safe flag reader must swallow."""


def _raise_down() -> object:
    raise _DownError


def _claimed_task() -> Task:
    ticket = Ticket.objects.create(issue_url="https://example.com/i/1", role=Ticket.Role.AUTHOR)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        phase="coding",
        execution_target=Task.ExecutionTarget.HEADLESS,
    )
    Task.objects.filter(pk=task.pk).update(execution_target=Task.ExecutionTarget.HEADLESS, status=Task.Status.CLAIMED)
    task.refresh_from_db()
    return task


_SESSION_MATCH = LimitMatch(phrase="five_hour", cause=LimitCause.SUBSCRIPTION_SESSION)
_CREDIT_MATCH = LimitMatch(phrase="out_of_credits", cause=LimitCause.API_CREDIT)


class TestEffectiveResetsAt(django.test.SimpleTestCase):
    def test_sdk_reset_wins(self) -> None:
        now = timezone.now()
        sdk = now + timedelta(hours=2)
        assert effective_resets_at(LimitCause.SUBSCRIPTION_SESSION, sdk, now) == sdk

    def test_session_horizon_fallback(self) -> None:
        now = timezone.now()
        assert effective_resets_at(LimitCause.SUBSCRIPTION_SESSION, None, now) == now + timedelta(hours=5)

    def test_credit_has_no_reset(self) -> None:
        now = timezone.now()
        assert effective_resets_at(LimitCause.API_CREDIT, None, now) is None

    def test_credit_ignores_an_sdk_reset(self) -> None:
        # An `overage` rejection maps to API_CREDIT yet can carry a top-level resets_at;
        # the cause has no time-based recovery, so the SDK value must NOT re-arm it.
        now = timezone.now()
        assert effective_resets_at(LimitCause.API_CREDIT, now + timedelta(hours=1), now) is None


class TestParkTaskOnLimitFlagOff(django.test.TestCase):
    def test_inert_when_flag_off(self) -> None:
        _set_autorecovery(on=False)
        task = _claimed_task()
        parked = park_task_on_limit(task, _SESSION_MATCH, sdk_resets_at=None, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert parked is None
        assert not UsageWindowState.objects.exists()
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED  # untouched — caller records the terminal FAILED


class TestParkTaskOnLimitFlagOn(django.test.TestCase):
    def setUp(self) -> None:
        _set_autorecovery(on=True)

    def test_session_limit_parks_task_and_records_window(self) -> None:
        now = timezone.now()
        task = _claimed_task()
        parked = park_task_on_limit(
            task, _SESSION_MATCH, sdk_resets_at=None, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now
        )
        assert parked is not None
        assert parked.error.startswith(LIMIT_PARKED_PREFIX)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING  # parked, NOT terminal FAILED
        assert task.not_before == now + timedelta(hours=5)
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.cause == LimitCause.SUBSCRIPTION_SESSION.value
        assert window.resets_at == now + timedelta(hours=5)

    def test_sdk_reset_timestamp_is_used(self) -> None:
        now = timezone.now()
        sdk_epoch = int((now + timedelta(hours=3)).timestamp())
        task = _claimed_task()
        park_task_on_limit(task, _SESSION_MATCH, sdk_resets_at=sdk_epoch, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == datetime.fromtimestamp(sdk_epoch, tz=UTC)

    def test_unparseable_sdk_epoch_falls_back_to_horizon(self) -> None:
        # A garbage/overflowing SDK resets_at is ignored — the cause horizon fills the gap.
        now = timezone.now()
        task = _claimed_task()
        park_task_on_limit(task, _SESSION_MATCH, sdk_resets_at=10**20, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == now + timedelta(hours=5)

    def test_credit_exhaustion_is_not_parked(self) -> None:
        # API-credit has no time-based recovery → nothing to re-arm to, so it stays a
        # terminal FAILED (the caller's fallback), never an indefinitely-parked task.
        task = _claimed_task()
        parked = park_task_on_limit(task, _CREDIT_MATCH, sdk_resets_at=None, lane=TaskAttempt.Lane.METERED)
        assert parked is None
        assert not UsageWindowState.objects.exists()

    def test_credit_with_an_sdk_overage_reset_is_still_not_parked(self) -> None:
        # An `overage` rejection maps to API_CREDIT but carries a top-level resets_at. Parking
        # it would spin the credit-exhausted lane (recover → re-dispatch → re-park) and never
        # tell the operator to add credits — so it stays a terminal FAILED, not a park.
        now = timezone.now()
        sdk_epoch = int((now + timedelta(hours=1)).timestamp())
        task = _claimed_task()
        parked = park_task_on_limit(task, _CREDIT_MATCH, sdk_resets_at=sdk_epoch, lane=TaskAttempt.Lane.METERED)
        assert parked is None
        assert not UsageWindowState.objects.exists()
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED  # untouched — caller records the terminal FAILED
        assert task.not_before is None

    def test_parked_attempt_excluded_from_repair_budget(self) -> None:
        from teatree.core.models.task_repair import phase_attempts  # noqa: PLC0415 — deferred (test-local)

        task = _claimed_task()
        park_task_on_limit(task, _SESSION_MATCH, sdk_resets_at=None, lane=TaskAttempt.Lane.SUBSCRIPTION)
        assert phase_attempts(task) == []


class TestFlagReadFailsSafeOff(django.test.TestCase):
    def test_config_read_exception_disables_autorecovery(self) -> None:
        # An unreadable flag must never silently change dispatch behaviour — fail-safe OFF.
        task = _claimed_task()
        with mock.patch.object(usage_window_mod, "get_effective_settings", _raise_down):
            assert (
                park_task_on_limit(task, _SESSION_MATCH, sdk_resets_at=None, lane=TaskAttempt.Lane.SUBSCRIPTION) is None
            )
        assert not UsageWindowState.objects.exists()


class TestAdmissionGuard(django.test.TestCase):
    def test_inert_when_flag_off(self) -> None:
        _set_autorecovery(on=False)
        now = timezone.now()
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        task = _claimed_task()
        assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION) is None
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    def test_parks_dispatch_while_window_active(self) -> None:
        _set_autorecovery(on=True)
        now = timezone.now()
        reset = now + timedelta(hours=5)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=reset,
            now=now,
        )
        task = _claimed_task()
        parked = maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == reset

    def test_no_window_lets_dispatch_through(self) -> None:
        _set_autorecovery(on=True)
        task = _claimed_task()
        assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION) is None

    def test_due_window_lets_dispatch_through(self) -> None:
        # The window's reset already passed (recovery will clear it) — let the dispatch try.
        _set_autorecovery(on=True)
        now = timezone.now()
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now - timedelta(minutes=1),
            now=now - timedelta(hours=5),
        )
        task = _claimed_task()
        assert maybe_park_for_active_window(task, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now) is None
