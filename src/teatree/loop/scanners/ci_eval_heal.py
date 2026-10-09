"""CI-eval heal scanner — flag that open heal sessions need advancing (#3201 PR-3a).

The scanner is a pure trigger: it emits ONE ``ci_eval_heal.advance`` signal iff at
least one :class:`~teatree.core.models.CiEvalHealSession` is still open (not GREEN /
HALTED). The mechanical handler (:func:`teatree.loop.mechanical_ci_eval_heal.advance_ci_eval_heal`)
then advances each open session one FSM step. When no session is open the scanner
returns nothing, so an enabled-but-idle loop is silent.

The durable ``CiEvalHealSession`` rows ARE the cross-tick state, so this scanner
needs no cadence gate or dedup lock of its own: an already-``AWAITING_CI`` session
is simply polled again next tick. Sessions are created only by an operator
(``t3 eval ci-heal open``) — the loop never discovers PR branches to touch on its
own.
"""

import logging
from dataclasses import dataclass

from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

#: The single signal kind this scanner emits — routed to the ``advance_ci_eval_heal``
#: mechanical handler via ``MECHANICAL_BY_KIND``.
CI_EVAL_HEAL_ADVANCE_KIND = "ci_eval_heal.advance"


@dataclass(slots=True)
class CiEvalHealScanner:
    """Emit an advance signal while any heal session is open; nothing when idle."""

    name: str = "ci_eval_heal"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models import CiEvalHealSession  # noqa: PLC0415 — deferred: ORM needs the app registry

        terminal = (CiEvalHealSession.State.GREEN, CiEvalHealSession.State.HALTED)
        open_count = CiEvalHealSession.objects.exclude(state__in=terminal).count()
        logger.debug("%s: %d open heal session(s)", self.name, open_count)
        if open_count == 0:
            return []
        return [
            ScanSignal(
                kind=CI_EVAL_HEAL_ADVANCE_KIND,
                summary=f"{open_count} CI-eval heal session(s) to advance",
                payload={"open_count": open_count},
            )
        ]
