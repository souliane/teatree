"""The CPU a worker tick spends, recorded onto its loop's cost EWMA (#4999)."""

import logging
import os
import resource

from django.db import DatabaseError

from teatree.core.models import Loop
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER

logger = logging.getLogger(__name__)


def process_cpu_seconds() -> float:
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def record_tick_cost(loop: str, *, found_work: bool) -> None:
    """Record this process's CPU against *loop*, only inside the worker's tick subprocess.

    Gated on the worker's marker so a by-hand or in-test run never skews the average, and a
    telemetry write that fails (a busy SQLite) logs and moves on rather than failing the tick.
    """
    if not os.environ.get(TICK_SUBPROCESS_ENV_MARKER):
        return
    try:
        Loop.objects.record_tick_cost(loop, cpu_seconds=process_cpu_seconds(), found_work=found_work)
    except DatabaseError:
        logger.warning("could not record the tick cost of loop %r", loop, exc_info=True)
