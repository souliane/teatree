"""Tick-piggyback safety net for the reactive slots (#1107 Prong B).

Defense-in-depth for the #1107 incident. Prong A fixes the headline root
cause (``current_session_id()`` could not resolve an owner in agent-driven
mode, so ``t3 loop claim`` hard-refused and the owner-gated reactive slots
were permanently dead). Prong B is the independent safety net: even in a
pure-cron / no-session deployment where Prong A still cannot resolve an
owner, a *won* ``t3 loop tick`` — the one beat that is guaranteed to keep
running — also drives the reactive Slack-answer cycle and the cheap
self-improve tier so user DMs still get :eyes:/answered and smells still
get recorded.

The piggyback runs ONLY on the won-owner success path of
``loop_tick.Command.handle`` (AFTER the ``loop-tick`` lease ``finally``,
AFTER the #1073 owner gate). A non-owner foreign-session SKIP must NOT
piggyback — that would re-open the #1073 hijack (a foreign session
draining the user's Slack DMs). ``loop_tick`` enforces that by calling
this only past the owner gate.

Each cycle is guarded by its own dedicated ``LoopLease`` CAS — the SAME
lease a real dedicated ``loop-slack-answer`` / ``loop-self-improve`` slot
acquires (``loop_slack_answer.py`` / ``loop_self_improve.py``). If a real
slot already holds it the piggyback's CAS loses and it skips, so the
#1014/#1075 dedicated fast path is never double-run. The lease is acquired
with a per-tick-unique owner and ``lease_seconds=<cadence>`` and is NEVER
released here: that makes the lease TTL double as the throttle — a
re-tick inside the cadence window finds the lease still held by the
previous tick's (different) owner, loses the CAS, and skips. Zero new
state, zero new columns.
"""

import logging
import os
import uuid

from teatree.loop.loop_cadences import _LOOP_OWNER_TTL_DEFAULT
from teatree.loop.loop_cadences import loop_owner_ttl_seconds as _loop_owner_ttl_seconds
from teatree.loop.loop_cadences import self_improve_cadence_seconds as _self_improve_cadence_seconds
from teatree.loop.loop_cadences import slack_answer_cadence_seconds as _slack_answer_cadence_seconds
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle

logger = logging.getLogger(__name__)

# The four pure-`os.environ` cadence readers moved DOWN to the
# `teatree.loop.loop_cadences` leaf (#2413 PR-4) so the statusline loop-line can
# reach them without deferring an import UP into this orchestration-top module.
# Re-exported here under their established private names so the `loop_tick` /
# `loops_tick` management commands and the existing tests keep their import path.
__all__ = [
    "_LOOP_OWNER_TTL_DEFAULT",
    "_loop_owner_ttl_seconds",
    "_self_improve_cadence_seconds",
    "_slack_answer_cadence_seconds",
    "run_piggyback_cycles",
]


def _piggyback_slack_answer() -> None:
    """Drive one reactive Slack-answer cycle behind the dedicated lease CAS."""
    from teatree.core.models import LoopLease  # noqa: PLC0415

    owner = f"tickpiggyback-{os.getpid()}-{uuid.uuid4().hex}"
    if not LoopLease.objects.acquire("loop-slack-answer", owner=owner, lease_seconds=_slack_answer_cadence_seconds()):
        return
    run_slack_answer_cycle()


def _piggyback_self_improve() -> None:
    """Drive one cheap-tier self-improve cycle behind the dedicated lease CAS."""
    from teatree.core.models import LoopLease  # noqa: PLC0415
    from teatree.loop.self_improve.schedule import Tier, run_tier  # noqa: PLC0415

    owner = f"tickpiggyback-{os.getpid()}-{uuid.uuid4().hex}"
    if not LoopLease.objects.acquire("loop-self-improve", owner=owner, lease_seconds=_self_improve_cadence_seconds()):
        return
    run_tier(Tier.CHEAP)


def run_piggyback_cycles() -> None:
    """Run the reactive cycles, each isolated so one failure cannot mask the others.

    Called from ``loop_tick.Command.handle`` on the won-owner success path
    only. Each cycle's broad ``except`` mirrors the loop's existing
    crash-isolation convention (``cycle.py`` / ``tick_recovery.py``): a
    safety net must never turn a transient cycle error into a failed tick.
    """
    try:
        _piggyback_slack_answer()
    except Exception as exc:  # noqa: BLE001 — a safety-net cycle must never fail the tick
        logger.warning("Tick-piggyback Slack-answer cycle failed: %s", exc)
    try:
        _piggyback_self_improve()
    except Exception as exc:  # noqa: BLE001 — a safety-net cycle must never fail the tick
        logger.warning("Tick-piggyback self-improve cycle failed: %s", exc)
    try:
        from teatree.loop.queue_drain import _piggyback_drain_queue  # noqa: PLC0415

        _piggyback_drain_queue()
    except Exception as exc:  # noqa: BLE001 — a safety-net cycle must never fail the tick
        logger.warning("Tick-piggyback queue-drain cycle failed: %s", exc)
