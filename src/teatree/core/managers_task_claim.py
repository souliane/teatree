"""Task-claim admission predicate + claim ordering (#3644 module-health carve).

Split out of :mod:`teatree.core.managers` (mirrors the ``managers_overlay`` /
``loop_lease_manager`` carves) so the "when is a task claimable, and in what
order" concern lives in one self-describing leaf. :mod:`teatree.core.managers`
re-exports :class:`ClaimOrder` and :func:`_claimable_now_q`, so existing
``from teatree.core.managers import …`` call sites (``loop.queue_drain``,
``core.tasks``, ``core.models.task_claim``) are unchanged.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q
from django.db.models.expressions import BaseExpression

from teatree.config import worker_is_quiescing
from teatree.core.process_freshness import code_behind_schema as _process_code_behind_schema
from teatree.core.schema_readiness import schema_admission_block_reason
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimOrder:
    """Optional ordering for :meth:`TaskManager.claim_next_pending`.

    Bundles the ``.annotate()`` kwargs and the resulting ``order_by`` fields so a
    caller can pick the claim order (admission priority: a queued TODO/followup
    before a new-ticket auto-start) through one parameter. The default claim path
    passes no ``ClaimOrder`` and stays plain oldest-``pk``.
    """

    annotations: dict[str, BaseExpression]
    order_by: tuple[str, ...]


def _claimable_now_q(now: datetime) -> Q:
    """The ``not_before`` admission predicate — a task is claimable now iff not window-parked.

    A null ``not_before`` (every task never limit-parked) or an elapsed one is claimable; a
    future ``not_before`` (a task parked behind an exhausted usage window, Directive #3)
    is skipped until the window re-arms. Shared by both claim paths so the gate can never
    drift between "is there work" and the actual claim.
    """
    return Q(not_before__isnull=True) | Q(not_before__lte=now)


def schema_behind_code() -> bool:
    """Deploy-order gate (#3901) — refuse admission while the DB lags the running code.

    `self_update` advances a live worker's HEAD on a cadence while applying the schema
    is a separate boot-time step, so the process can hold code whose models the control
    DB does not carry. Dispatching into that window crashes the agent on the first
    missing relation; deferring the claim costs one tick and self-heals the moment the
    migrations land.
    """
    reason = schema_admission_block_reason()
    if reason:
        # Throttled: this is the claim hot path and the refusal repeats once per
        # refused claim for as long as the park lasts. The first refusal (and the
        # first after each quiet window) warns, the rest go to debug — the park
        # stays visible at a bounded cadence instead of drowning the log.
        warn_throttled(logger, "task-claim:schema-behind", "task claim deferred: %s", reason)
    return bool(reason)


def code_behind_schema() -> bool:
    """The MIRROR of :func:`schema_behind_code` (#4387) — refuse while THIS process lags the DB.

    The asymmetry is the whole point, and it is why both names sit adjacent here: the
    checked direction (schema behind code) mostly fails on READ and self-heals one tick
    after ``init`` migrates, while the unchecked one fails DESTRUCTIVELY — the task is
    claimed, an agent runs to completion, and the result is discarded at the record step
    because the in-memory model class predates a ``NOT NULL`` column. Never re-add only
    one of the two.
    """
    reason = _process_code_behind_schema()
    if reason:
        warn_throttled(logger, "task-claim:code-behind", "task claim deferred: %s", reason)
    return bool(reason)


def claim_admission_block_reason() -> str:
    """Why NO task may be claimed right now, or ``""`` to admit — the ONE admission composition.

    Both claim paths (``claimable`` and ``claim_next_pending``) call this rather than
    restating the boolean, so a third admission direction can never be added to one site
    and forgotten at the other — which is exactly how #4387's skew went unguarded.
    """
    if worker_is_quiescing():
        return "this worker is quiescing for a rolling deploy"
    if schema_behind_code():
        return "the control DB is behind this code"
    if code_behind_schema():
        return "this process is behind the applied schema"
    return ""
