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
"""

import logging
from datetime import datetime

from django.utils import timezone

from teatree.config import AgentHarnessProvider
from teatree.core.models import TaskAttempt, UsageWindowState
from teatree.credential_config import api_key_lane_available
from teatree.llm.anthropic_limits import LimitCause

logger = logging.getLogger(__name__)

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


def subscription_lane_exhausted(*, now: datetime | None = None) -> bool:
    """True while an uncleared plan-exhaustion window still covers the subscription lane."""
    moment = now or timezone.now()
    window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION)
    return window is not None and window.cause in EXHAUSTION_CAUSES and not window.should_clear(moment)


def resolve_credential_provider(
    configured: AgentHarnessProvider | None,
    *,
    scope: str,
    now: datetime | None = None,
) -> AgentHarnessProvider | None:
    """The CONCRETE provider a dispatch authenticates with, given the *configured* one.

    Every value but the composite is returned unchanged, so an existing deployment resolves
    byte-identically. The composite answers :attr:`~teatree.config.AgentHarnessProvider.API_KEY`
    only while BOTH halves hold — the plans are spent AND the metered lane is usable in
    *scope* — and :attr:`~teatree.config.AgentHarnessProvider.SUBSCRIPTION_OAUTH` otherwise.

    The unusable-meter fallback is what makes the composite safe to configure before an API
    key is: the lane then parks behind its window exactly as a plans-only deployment does,
    so flipping the setting can never convert a quiesce into a terminal failure. It is
    warned rather than silent, because a factory that believes it has a fallback and does
    not is worth saying out loud.
    """
    if configured is not AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY:
        return configured
    if not subscription_lane_exhausted(now=now):
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
