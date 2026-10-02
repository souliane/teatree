"""Which Layer-2 lane a dispatch authenticates through — the ONE authority (#4816).

The runner resolved this for ATTRIBUTION; the governor never resolved it at all and so
judged every dispatch against the Anthropic subscription fleet, including the metered
ones that fleet says nothing about. Both now read one function, because a second copy of
the mapping is a second opinion about the very lane a refusal names.

**The governor reads the PROVIDER, never the harness registry.** ``teatree.core`` may not
import ``teatree.agents`` (tach), so :func:`configured_dispatch_lane` cannot ask which
transport is registered — which is correct rather than merely permitted: an unset provider
pin means the ambient credential decided, which is genuinely unobservable from here, and
the resulting unattributed lane keeps today's behaviour byte-for-byte.
"""

import logging

from teatree.config import AgentHarnessProvider
from teatree.core.models.task_attempt import TaskAttempt

logger = logging.getLogger(__name__)

#: souliane/teatree#657: the lane each provider authenticates through. Every BYOK key —
#: the Anthropic API key under either binding, and the OpenAI-compatible router key — is
#: metered; only the plan's OAuth token draws on the subscription fleet.
LANE_BY_PROVIDER: dict[AgentHarnessProvider, str] = {
    AgentHarnessProvider.SUBSCRIPTION_OAUTH: TaskAttempt.Lane.SUBSCRIPTION,
    AgentHarnessProvider.API_KEY: TaskAttempt.Lane.METERED,
    AgentHarnessProvider.OPENAI_COMPATIBLE: TaskAttempt.Lane.METERED,
    AgentHarnessProvider.ANTHROPIC_API: TaskAttempt.Lane.METERED,
}


def dispatch_lane(*, provider: AgentHarnessProvider | None, metered_harness: bool) -> str:
    """The lane a dispatch on *provider* rides, or ``""`` when unattributed.

    *metered_harness* is the transport's own ``metered_lane`` capability, which FIXES the
    lane whatever the provider pin says. A provider with no entry reads unattributed
    rather than raising: a future enum member added without a mapping here must not
    surface as a ``KeyError`` that records an already-billed run as FAILED.
    """
    if metered_harness:
        return TaskAttempt.Lane.METERED
    if provider is None:
        return ""
    return LANE_BY_PROVIDER.get(provider, "")


def configured_dispatch_lane() -> str:
    """The lane the CONFIGURED provider pin resolves to — the governor's read.

    An unreadable setting is unattributed, the direction every unknown in the admission
    path takes: a probe that cannot answer must not be able to move a lane's budget.
    """
    try:
        provider = _configured_provider()
    except Exception:
        logger.exception("agent_harness_provider unreadable — treating the dispatch lane as unattributed")
        return ""
    return dispatch_lane(provider=provider, metered_harness=False)


def _configured_provider() -> AgentHarnessProvider | None:
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    return get_effective_settings().agent_harness_provider


__all__ = ["LANE_BY_PROVIDER", "configured_dispatch_lane", "dispatch_lane"]
