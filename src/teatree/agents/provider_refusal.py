"""A metered provider's hard 401/403 refusal, as the rejected window the driver already drains."""

import re
import time
import uuid
from datetime import UTC, datetime

from claude_agent_sdk import RateLimitEvent
from claude_agent_sdk.types import RateLimitInfo
from pydantic_ai.exceptions import ModelHTTPError

from teatree.agents.runner_failure_taxonomy import HARD_REFUSAL_STATUSES
from teatree.llm.anthropic_limits import believable_refusal_reset

#: An ISO-8601 instant anywhere in a refusal body — the fallback when the router sends no
#: ``Retry-After``. The observed shape is prose: ``"token cycle spend limit reached, resets
#: at 2026-09-21T00:00:00Z"``, so the instant is extracted rather than parsed off a field.
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def _refusal_resets_at(exc: ModelHTTPError) -> int | None:
    """When the provider says its refusal lifts, as a Unix timestamp — or ``None``.

    Two rungs, structured first: ``Retry-After`` (which pydantic_ai already parses in both
    its delta-seconds and HTTP-date forms), then an ISO-8601 instant in the body. BOTH are
    bounded by :func:`believable_refusal_reset`, because neither is a window teatree can
    verify and nothing downstream bounds a park at all: a body's first ISO-8601 instant is
    as often the request's own ``created`` stamp as the reset, and a key ``expires_at``
    parks the lane for years. Outside the band the answer is ``None``, which is SAFE
    rather than a failure — ``effective_resets_at`` falls back to the cause's one-hour
    horizon, so a rejected parse costs one extra hour of park and an accepted one is
    capped at :data:`~teatree.llm.anthropic_limits.REFUSAL_RESET_CEILING`.
    """
    now = time.time()
    if exc.retry_after is not None:
        return believable_refusal_reset(now + exc.retry_after, now=now)
    found = _ISO_INSTANT.search(str(exc.body or ""))
    if found is None:
        return None
    try:
        parsed = datetime.fromisoformat(found.group())
    except ValueError:
        return None
    aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return believable_refusal_reset(aware.timestamp(), now=now)


def hard_refusal_event(exc: ModelHTTPError, *, session_id: str) -> RateLimitEvent | None:
    """The rejected window a 401/403 carries, or ``None`` for every other status.

    A hard refusal parks the LANE — so it rides the channel the driver already drains
    (``_collect`` → ``outcome.rate_limit_info`` → ``UsageWindowState``) rather than a new
    one. ``rate_limit_type`` stays unset: the provider named no Anthropic window, and
    ``limit_match`` classifies this from the status before it ever reads the typed field.
    """
    if exc.status_code not in HARD_REFUSAL_STATUSES:
        return None
    info = RateLimitInfo(status="rejected", resets_at=_refusal_resets_at(exc), raw={"status": exc.status_code})
    return RateLimitEvent(rate_limit_info=info, uuid=uuid.uuid4().hex, session_id=session_id)
