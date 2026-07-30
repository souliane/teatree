"""CI-eval heal mechanical handler — the executor for ``ci_eval_heal.advance`` (#3201 PR-3a).

The scanner (:mod:`teatree.loop.scanners.ci_eval_heal`) only FLAGS that open heal
sessions exist; this handler runs the actual one-step advance over each of them via
:func:`teatree.loop.ci_eval_heal_advance.advance_open_sessions` — dispatch a CI eval,
poll a run, and GREEN / HALT + escalate, never a fix. Best-effort: any failure logs
and is swallowed so a stalled ``gh`` call never aborts the loop tick (the advancer
already swallows per-session errors; this guards the pass itself).
"""

import logging

from teatree.loop.ci_eval_heal_advance import advance_open_sessions
from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)


def advance_ci_eval_heal(payload: ActionPayload) -> None:
    """Advance every open CI-eval heal session one FSM step — best-effort, never raises into the loop.

    The ``payload`` carries only the scanner's ``open_count`` (a legibility hint);
    the advancer re-reads the open sessions itself, so a session opened/closed
    between scan and execute is handled correctly.
    """
    flagged = payload.get("open_count", "?")
    try:
        run = advance_open_sessions()
    except Exception:
        logger.exception("advance_ci_eval_heal: advance pass failed")
        return
    logger.info(
        "advance_ci_eval_heal: %s flagged, advanced %d session(s), %d error(s)",
        flagged,
        len(run.outcomes),
        len(run.errors),
    )


__all__ = ["advance_ci_eval_heal"]
