"""Ride the Claude subscription plans; reach for the metered API only once they are spent.

``agent_harness_provider=subscription_then_api_key`` is the owner's third value beside the
two that already exist (plans-only and API-only). It is a POLICY, not a credential: this
module resolves it ONCE per dispatch into one of those two concrete providers and never
returns the composite, so :data:`~teatree.core.admission.dispatch_lane.LANE_BY_PROVIDER`, the child-env
resolver and cost accounting keep seeing only values they already handle.

"Every plan is spent" is one durable row, not a probe: an uncleared
:class:`~teatree.core.models.UsageWindowState` on the ``subscription`` lane whose cause is
one of :data:`EXHAUSTION_CAUSES` and whose reset is still ahead. A transient 429 records
``rate_limit``, which is deliberately absent from that set — a throttle waits out its five
minutes on the plan instead of moving the whole factory onto the meter.

A metered key the provider refuses for credit is spent for ``HEALTH_TTL`` (:func:`spend_metered_key`):
the next dispatch rotates to another key, and with none left the task waits for the plan window's reset.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.core.models import TaskAttempt, UsageWindowState
from teatree.credential_config import api_key_lane_available, oauth_lane_available, record_api_key_exhausted
from teatree.llm.anthropic_limits import LimitCause

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from teatree.core.models import Task

PLANS_ONLY_MARKER = "[plans-only-retry]"


def plans_only(task: "Task | None") -> bool:
    return task is not None and PLANS_ONLY_MARKER in task.execution_reason


#: The ``UsageWindowState.cause`` recorded when the WHOLE subscription lane parked because
#: every configured account drained — distinct from a single-account cause for audit and
#: notify wording. Owned here because this module is what READS it as plan exhaustion;
#: :mod:`teatree.agents.usage_window` imports it so writer and reader share one spelling.
ALL_ACCOUNTS_EXHAUSTED_CAUSE = "all_accounts_exhausted"

#: The causes that mean the PLANS are spent rather than momentarily throttled. Everything
#: else a subscription-lane window can carry (``rate_limit``, ``api_credit``,
#: ``provider_budget``, ``leak_blocked``) leaves the lane on plans.
EXHAUSTION_CAUSES: frozenset[str] = frozenset(
    {
        LimitCause.SUBSCRIPTION_SESSION.value,
        LimitCause.SUBSCRIPTION_WEEKLY.value,
        ALL_ACCOUNTS_EXHAUSTED_CAUSE,
    },
)


def open_plan_window(now: datetime | None = None) -> UsageWindowState | None:
    """The uncleared plan-exhaustion window still covering the subscription lane, or ``None``."""
    moment = now or timezone.now()
    window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
    return (
        window if window is not None and window.cause in EXHAUSTION_CAUSES and not window.should_clear(moment) else None
    )


def subscription_lane_exhausted(*, scope: str = "", now: datetime | None = None) -> bool:
    """True while an uncleared plan-exhaustion window still covers the subscription lane."""
    window = open_plan_window(now)
    return window is not None and (
        window.cause != ALL_ACCOUNTS_EXHAUSTED_CAUSE or not oauth_lane_available(scope=scope)
    )


def spend_metered_key(*, scope: str, lane: str, signer: str, moment: datetime) -> datetime | None:
    """Mark the metered key behind a credit refusal spent, and say when the task may run again.

    Called once a dispatch was refused for credit. ``None`` leaves the refusal to today's flow:
    any lane but the metered one, any provider but the composite, or an ambient key (no
    *signer*) with no open plan window. *moment* means a key or the plans can serve now; a later
    instant is the open plan window's reset, the earliest anything can. The signer is the key
    the dispatch signed with, never the scope's shared active pick, which may name a bystander.
    """
    configured = get_effective_settings(scope or None).agent_harness_provider
    if lane != TaskAttempt.Lane.METERED or configured is not AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY:
        return None
    window = open_plan_window(moment)
    reset = window.resets_at if window is not None else None
    if not signer:
        return reset
    record_api_key_exhausted(signer, now=moment)
    return moment if reset is None or api_key_lane_available(scope=scope) else reset


def resolve_credential_provider(
    configured: AgentHarnessProvider | None,
    *,
    scope: str,
    now: datetime | None = None,
    plans_only: bool = False,
) -> AgentHarnessProvider | None:
    """The CONCRETE provider a dispatch authenticates with, given the *configured* one.

    Every value but the composite is returned unchanged except an unset provider on an
    explicit plans-only retry. The composite answers :attr:`~teatree.config.AgentHarnessProvider.API_KEY`
    only while BOTH halves hold — the plans are spent AND the metered lane is usable in
    *scope* — and :attr:`~teatree.config.AgentHarnessProvider.SUBSCRIPTION_OAUTH` otherwise.

    The unusable-meter fallback is what makes the composite safe to configure before an API
    key is: the lane then parks behind its window exactly as a plans-only deployment does,
    so flipping the setting can never convert a quiesce into a terminal failure. It is
    warned rather than silent, because a factory that believes it has a fallback and does
    not is worth saying out loud.
    """
    if plans_only and configured in {None, AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY}:
        return AgentHarnessProvider.SUBSCRIPTION_OAUTH
    if configured is not AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY:
        return configured
    if not subscription_lane_exhausted(scope=scope, now=now):
        return AgentHarnessProvider.SUBSCRIPTION_OAUTH
    if api_key_lane_available(scope=scope):
        return AgentHarnessProvider.API_KEY
    logger.warning(
        "agent_harness_provider=%s, the subscription plans are exhausted, and no metered "
        "credential is usable for scope %r (anthropic_api_key_pass_paths routes nothing "
        "usable and ANTHROPIC_API_KEY is unset or spent); the lane stays on the plans and "
        "parks until the window re-arms",
        AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY.value,
        scope,
    )
    return AgentHarnessProvider.SUBSCRIPTION_OAUTH
