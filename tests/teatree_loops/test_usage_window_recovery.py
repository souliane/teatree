"""``usage_window_recovery`` — the self-rescheduling re-arm chain (Directive #3).

The crux behavioural contract: a parked usage window is cleared (and its parked tasks
released, the loop pumped, one Slack line posted) ONLY once the reset instant has passed —
never before — and the whole path is inert while ``limit_autorecovery_enabled`` is OFF.
"""

from datetime import datetime, timedelta
from unittest import mock

import django.test
from django.utils import timezone

from teatree.core.models import BotPing, Session, Task, Ticket, UsageWindowState
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.task_attempt import TaskAttempt
from teatree.llm.anthropic_limits import LimitCause
from teatree.loops.usage_window_recovery import (
    ensure_usage_window_recovery_chain,
    recover_windows,
    usage_window_recovery,
)


def _set_autorecovery(*, on: bool) -> None:
    ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=on)


class _DownError(RuntimeError):
    """A stand-in dependency failure the best-effort recovery paths must swallow."""


def _raise_down(*_args: object, **_kwargs: object) -> object:
    raise _DownError


def _parked_task(not_before: datetime) -> Task:
    ticket = Ticket.objects.create(issue_url="https://example.com/i/1", role=Ticket.Role.AUTHOR)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, phase="coding")
    Task.objects.filter(pk=task.pk).update(
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.PENDING,
        not_before=not_before,
    )
    task.refresh_from_db()
    return task


def _due_window() -> UsageWindowState:
    now = timezone.now()
    return UsageWindowState.record_limit(
        lane=TaskAttempt.Lane.SUBSCRIPTION,
        cause=LimitCause.SUBSCRIPTION_SESSION.value,
        resets_at=now - timedelta(minutes=1),
        now=now - timedelta(hours=5),
    )


class TestReArmsOnlyAfterReset(django.test.TestCase):
    """The anti-vacuous contract: no clear/release before the reset, both after it."""

    def test_before_reset_leaves_window_parked(self) -> None:
        now = timezone.now()
        reset = now + timedelta(hours=5)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=reset,
            now=now,
        )
        task = _parked_task(not_before=reset)

        outcome = recover_windows(now + timedelta(hours=4, minutes=59))

        assert outcome.cleared == []
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION) is not None
        # The task is still parked — not claimable while its not_before is in the future.
        assert Task.objects.claim_next_pending(claimed_by="w") is None
        task.refresh_from_db()
        assert task.not_before == reset

    def test_at_reset_clears_window_and_releases_task(self) -> None:
        now = timezone.now()
        reset = now + timedelta(hours=5)
        window = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=reset,
            now=now,
        )
        task = _parked_task(not_before=reset)

        outcome = recover_windows(reset)

        assert outcome.cleared == [window.pk]
        assert outcome.released == 1
        window.refresh_from_db()
        assert window.cleared_at is not None
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION) is None
        task.refresh_from_db()
        assert task.not_before is None  # released — immediately claimable
        assert Task.objects.claim_next_pending(claimed_by="w") is not None

    def test_recovery_posts_one_slack_line(self) -> None:
        now = timezone.now()
        reset = now + timedelta(hours=5)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=reset,
            now=now,
        )
        _parked_task(not_before=reset)
        recover_windows(reset)
        assert BotPing.objects.filter(idempotency_key__startswith="usage_window_recovered:").exists()

    def test_all_accounts_exhausted_park_auto_resumes_at_reset(self) -> None:
        # A task parked because every configured account drained must auto-resume at its reset.
        from teatree.agents.usage_window import park_task_on_all_exhausted  # noqa: PLC0415 — test-local

        _set_autorecovery(on=True)
        now = timezone.now()
        reset = now + timedelta(hours=2)
        task = _parked_task(not_before=now)  # placeholder; the park below sets the real gate
        Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED, not_before=None)
        task.refresh_from_db()

        parked = park_task_on_all_exhausted(task, resets_at=reset, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == reset

        assert recover_windows(now + timedelta(hours=1)).released == 0, "still parked before the reset"
        outcome = recover_windows(reset)
        assert outcome.released == 1
        task.refresh_from_db()
        assert task.not_before is None, "auto-resumed at the reset — claimable again"

    def test_credit_window_is_never_auto_cleared(self) -> None:
        # A null-reset (API-credit) window has no time-based recovery — never cleared here.
        now = timezone.now()
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.METERED,
            cause=LimitCause.API_CREDIT.value,
            resets_at=None,
            now=now,
        )
        outcome = recover_windows(now + timedelta(days=30))
        assert outcome.cleared == []
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED) is not None


class TestInertWhenFlagOff(django.test.TestCase):
    def test_task_body_no_ops_and_does_not_clear(self) -> None:
        _set_autorecovery(on=False)
        now = timezone.now()
        window = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now - timedelta(minutes=1),  # already due
            now=now - timedelta(hours=5),
        )
        result = usage_window_recovery.func()
        assert result.get("disabled")
        window.refresh_from_db()
        assert window.cleared_at is None  # untouched while the flag is off
        assert not BotPing.objects.exists()

    def test_task_body_recovers_when_flag_on(self) -> None:
        _set_autorecovery(on=True)
        now = timezone.now()
        window = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now - timedelta(minutes=1),  # already due
            now=now - timedelta(hours=5),
        )
        result = usage_window_recovery.func()
        assert result.get("cleared") == 1
        window.refresh_from_db()
        assert window.cleared_at is not None


# The suite's default TASKS backend does not persist enqueues; the real DatabaseBackend
# (mirroring test_timer_reconciler's `_DB_TASKS`) is required to assert a queued successor.
_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestChainScheduling(django.test.TestCase):
    """The self-rescheduling enqueue plumbing — asserts post-``enqueue()`` READY rows."""

    def setUp(self) -> None:
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

        DBTaskResult.objects.all().delete()

    def test_ensure_chain_seeds_one_pending_and_is_idempotent(self) -> None:
        ensure_usage_window_recovery_chain()
        ensure_usage_window_recovery_chain()
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred import (cycle-safe / task-body)
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

        pending = DBTaskResult.objects.filter(
            task_path=usage_window_recovery.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1

    def test_self_dedups_when_a_recovery_is_already_pending(self) -> None:
        _set_autorecovery(on=True)
        ensure_usage_window_recovery_chain()  # one pending recovery already carries the chain
        assert usage_window_recovery.func() == {"deduped": 1}

    def test_reschedules_itself_after_a_pass(self) -> None:
        _set_autorecovery(on=True)
        usage_window_recovery.func()
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred import (cycle-safe / task-body)
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

        assert DBTaskResult.objects.filter(
            task_path=usage_window_recovery.module_path, status=TaskResultStatus.READY
        ).exists()


class TestSideEffectsAreBestEffort(django.test.TestCase):
    def test_pump_refines_the_registered_loop_timers(self) -> None:
        calls: list[str] = []

        def _record(name: str, *, run_after: object) -> None:
            calls.append(name)

        with (
            mock.patch("teatree.loops.timer_reconciler.timer_chain_loop_names", return_value={"dispatch"}),
            mock.patch("teatree.loops.timer_chains.refine_successor", _record),
        ):
            _due_window()
            recover_windows(timezone.now())
        assert calls == ["dispatch"]

    def test_notify_failure_never_breaks_recovery(self) -> None:
        window = _due_window()
        with mock.patch("teatree.core.notify.notify_user", _raise_down):
            outcome = recover_windows(timezone.now())
        assert outcome.cleared == [window.pk]  # cleared despite the notify blowing up

    def test_pump_failure_never_breaks_recovery(self) -> None:
        window = _due_window()
        with mock.patch("teatree.loops.timer_reconciler.timer_chain_loop_names", _raise_down):
            outcome = recover_windows(timezone.now())
        assert outcome.cleared == [window.pk]  # cleared despite the loop pump blowing up

    def test_config_read_failure_disables_the_task_body(self) -> None:
        # ``_autorecovery_enabled`` defers ``from teatree.config import get_effective_settings``,
        # so the source attribute is the patch target — a read failure fails safe to OFF.
        _due_window()
        with mock.patch("teatree.config.get_effective_settings", _raise_down):
            assert usage_window_recovery.func() == {"disabled": 1}
