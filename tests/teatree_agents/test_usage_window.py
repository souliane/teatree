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
from teatree.agents.usage_window import (
    effective_resets_at,
    maybe_park_for_active_window,
    park_or_rotate_on_limit,
    park_task_on_all_exhausted,
    park_task_on_limit,
)
from teatree.core.models import (
    LIMIT_PARKED_PREFIX,
    AnthropicActivePick,
    AnthropicTokenUsage,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    UsageWindowState,
)
from teatree.core.models.anthropic_token_usage import REJECTED_STATUS, TokenHealthReading
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
_WEEKLY_MATCH = LimitMatch(phrase="seven_day", cause=LimitCause.SUBSCRIPTION_WEEKLY)
_CREDIT_MATCH = LimitMatch(phrase="out_of_credits", cause=LimitCause.API_CREDIT)
_RATE_LIMIT_MATCH = LimitMatch(phrase="rate limit", cause=LimitCause.RATE_LIMIT)

_OAUTH_SETTING = "anthropic_oauth_pass_paths"


def _seed_healthy(pass_path: str) -> None:
    """Cache *pass_path* as a fresh, healthy account so the selector routes to it with no probe."""
    AnthropicTokenUsage.objects.record(
        pass_path,
        TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.1,
            utilization_7d=0.1,
            status_5h="allowed",
            status_7d="allowed",
            reset_5h=None,
            reset_7d=None,
        ),
        now=timezone.now(),
    )


def _seed_exhausted(pass_path: str, *, reset: datetime) -> None:
    """Cache *pass_path* as a fresh, exhausted account (7d rejected until *reset*)."""
    AnthropicTokenUsage.objects.record(
        pass_path,
        TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.1,
            utilization_7d=1.0,
            status_5h="allowed",
            status_7d=REJECTED_STATUS,
            reset_5h=None,
            reset_7d=reset,
        ),
        now=timezone.now(),
    )


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


class TestParkOrRotateOnLimit(django.test.TestCase):
    """Multi-account #C1: a mid-run subscription limit ROTATES to a healthy account before parking.

    One account hitting its 5h/weekly window must NOT park the whole subscription lane while
    other accounts are healthy — that stranded the healthy accounts. The reactive handler
    records the current account exhausted and re-consults the selector: another healthy account
    → REQUEUE (rotate, no lane park); every account spent → park the lane for auto-resume.
    """

    def setUp(self) -> None:
        _set_autorecovery(on=True)

    def test_rotates_to_a_healthy_account_without_parking_the_lane(self) -> None:
        # account-1 hit its 5h limit mid-run; account-2 is healthy → the next dispatch must
        # route to account-2 and NO usage-window (lane park) is recorded.
        now = timezone.now()
        reset = now + timedelta(hours=3)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth", "acct/3/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")  # the account that hit the limit
        _seed_healthy("acct/2/oauth")
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task,
            _SESSION_MATCH,
            sdk_resets_at=int(reset.timestamp()),
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
        )

        assert parked is not None, "the task is requeued (an audit attempt is recorded), never failed"
        assert parked.error.startswith(LIMIT_PARKED_PREFIX)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "requeued to rotate, NOT terminal FAILED"
        assert task.not_before is not None
        assert task.not_before <= now, "claimable immediately on the next tick"
        assert not UsageWindowState.objects.exists(), "no lane park while a healthy account remains — C1"
        assert AnthropicActivePick.objects.pick_for("oauth", "") == "acct/2/oauth", "the sticky pick rotated"
        exhausted = AnthropicTokenUsage.objects.get(pass_path="acct/1/oauth")
        assert exhausted.is_exhausted, "the spent account is recorded exhausted so the selector routes off it"

    def test_all_accounts_exhausted_parks_the_lane_keyed_on_earliest_reset(self) -> None:
        # account-1 hits its limit and the other accounts are ALREADY exhausted → there is no
        # account to rotate to, so the whole lane parks (auto-resume at the earliest reset).
        now = timezone.now()
        soon = now + timedelta(hours=1)
        later = now + timedelta(hours=4)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")
        _seed_exhausted("acct/2/oauth", reset=soon)
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task,
            _WEEKLY_MATCH,
            sdk_resets_at=int(later.timestamp()),  # account-1's own reset — later than account-2's
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
        )

        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "parked, not FAILED"
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None, "all accounts spent → the whole lane parks"
        # parked until the EARLIEST account frees up, not account-1's later reset
        assert window.resets_at == soon
        assert task.not_before == soon

    def test_single_account_still_parks_the_lane_as_today(self) -> None:
        # No regression for the single-account deployment: its only account hitting the limit
        # has nowhere to rotate, so it parks the lane exactly like the pre-rotation behaviour.
        now = timezone.now()
        reset = (now + timedelta(hours=5)).replace(microsecond=0)  # epoch round-trips whole seconds
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["only/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "only/oauth")
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task,
            _SESSION_MATCH,
            sdk_resets_at=int(reset.timestamp()),
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=now,
        )

        assert parked is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == reset
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == reset

    def test_transient_rate_limit_parks_the_lane_without_rotating(self) -> None:
        # A transient 429 is lane-wide, not account-specific — it parks the lane (5-min horizon)
        # and never consults the per-account selector, so no account is marked exhausted.
        now = timezone.now()
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")
        task = _claimed_task()

        parked = park_or_rotate_on_limit(
            task, _RATE_LIMIT_MATCH, sdk_resets_at=None, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now
        )

        assert parked is not None
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.cause == LimitCause.RATE_LIMIT.value
        # no account rotation for a lane-wide 429
        assert not AnthropicTokenUsage.objects.filter(pass_path="acct/1/oauth").exists()

    def test_inert_when_flag_off(self) -> None:
        _set_autorecovery(on=False)
        now = timezone.now()
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["acct/1/oauth", "acct/2/oauth"])
        AnthropicActivePick.objects.set_pick("oauth", "", "acct/1/oauth")
        task = _claimed_task()
        assert (
            park_or_rotate_on_limit(
                task,
                _SESSION_MATCH,
                sdk_resets_at=int((now + timedelta(hours=3)).timestamp()),
                lane=TaskAttempt.Lane.SUBSCRIPTION,
                now=now,
            )
            is None
        ), "flag off → caller records the terminal FAILED, byte-identical to today"
        assert not UsageWindowState.objects.exists()
        assert not AnthropicTokenUsage.objects.exists()


class TestParkTaskOnAllExhausted(django.test.TestCase):
    """Multi-account #C2: every account drained → PARK the lane for auto-resume, never a human ping."""

    def test_parks_the_lane_keyed_on_the_earliest_reset(self) -> None:
        _set_autorecovery(on=True)
        now = timezone.now()
        reset = now + timedelta(hours=2)
        task = _claimed_task()
        parked = park_task_on_all_exhausted(task, resets_at=reset, lane=TaskAttempt.Lane.SUBSCRIPTION, now=now)
        assert parked is not None
        assert parked.error.startswith(LIMIT_PARKED_PREFIX)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "PARKED, not FAILED — no human escalation"
        assert task.not_before == reset
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
        assert window is not None
        assert window.resets_at == reset

    def test_inert_when_flag_off(self) -> None:
        _set_autorecovery(on=False)
        task = _claimed_task()
        assert (
            park_task_on_all_exhausted(task, resets_at=timezone.now() + timedelta(hours=1), lane="subscription") is None
        )
        assert not UsageWindowState.objects.exists()

    def test_no_reset_is_not_parked(self) -> None:
        _set_autorecovery(on=True)
        task = _claimed_task()
        assert park_task_on_all_exhausted(task, resets_at=None, lane="subscription") is None
        assert not UsageWindowState.objects.exists()

    def test_an_already_elapsed_reset_is_not_parked(self) -> None:
        """A park keyed on a past instant is dead on arrival — refuse it.

        The recovery chain would clear such a window on its very next tick and DM the owner
        "usage window restored"; a caller that keeps re-deriving an elapsed reset therefore
        floods the owner at the poll cadence instead of surfacing the real failure.
        """
        _set_autorecovery(on=True)
        now = timezone.now()
        task = _claimed_task()

        parked = park_task_on_all_exhausted(
            task, resets_at=now - timedelta(seconds=1), lane=TaskAttempt.Lane.SUBSCRIPTION, now=now
        )

        assert parked is None, "the caller falls back to its terminal path rather than parking"
        assert not UsageWindowState.objects.exists(), "no self-clearing window is written"
