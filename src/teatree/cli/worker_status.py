"""The lines and JSON fields ``t3 worker status`` reports beside the flock and the timers."""

import logging
from typing import TYPE_CHECKING

from teatree.loop.drain import quiesce_status

if TYPE_CHECKING:
    from teatree.core.agent_admission import HeadlessAdmissionJson
    from teatree.loop.drain import QuiescePayload
    from teatree.loops.loop_staleness import LoopHealth
    from teatree.utils.singleton import HolderRecord

logger = logging.getLogger(__name__)


def admission_line(health: "LoopHealth") -> str:
    """The fleet's stop condition: does the active preset admit any loop at all?"""
    verdict = health.admission
    state = "admits work" if health.fleet_admits else "admits ZERO loops — the fleet is stopped"
    return f"preset {verdict.mode!r} (source={verdict.source}) {state}"


def holder_lines(record: "HolderRecord | None") -> list[str]:
    """Where the flock holder is, and a pointer to the gate when it should not be there.

    ``worker: RUNNING`` is equally true of a singleton held from OUTSIDE the deployment
    (#3976), which is how that starvation stayed invisible: the flock genuinely is held
    and the loops genuinely do tick, driven by a process this service can never become.
    """
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        DEPLOYMENT_WORKER_ROLE,
        current_context,
    )

    if record is None:
        return []
    lines = [f"worker holder: PID {record.pid} in {record.context.describe()}"]
    mine = current_context()
    if mine.role and record.context.role != DEPLOYMENT_WORKER_ROLE:
        lines.append(
            f"WARN  that holder is not this deployment's {DEPLOYMENT_WORKER_ROLE} service — the deployed "
            "worker cannot start while it lives. Run `t3 doctor check` (#3976)."
        )
    return lines


def agent_admission_report() -> "tuple[str, HeadlessAdmissionJson | None]":
    """A failed admission read reports "unavailable" — it must never cost the worker report deploy.sh reads."""
    from teatree.core.agent_admission import (  # noqa: PLC0415 (deferred: no Django/DB at CLI import)
        headless_admission_status,
    )

    try:
        status = headless_admission_status()
    except Exception as exc:
        logger.debug("agent admission status unreadable", exc_info=True)
        first_line = next(iter(str(exc).splitlines()), "")
        return f"agent admission: unavailable ({type(exc).__name__}: {first_line})", None
    return status.line(), status.as_json()


def deploy_drain_report() -> "tuple[str, QuiescePayload | None]":
    """A failed quiesce read reports "unavailable" — it must never cost the worker report deploy.sh reads."""
    try:
        drain = quiesce_status()
    except Exception as exc:
        logger.debug("deploy drain status unreadable", exc_info=True)
        return f"deploy drain: unavailable ({type(exc).__name__})", None
    return (drain.status_line(), drain.as_json()) if drain is not None else ("", None)
