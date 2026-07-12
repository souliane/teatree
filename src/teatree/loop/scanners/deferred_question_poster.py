"""Scanner that mirrors un-mirrored ``DeferredQuestion`` rows to Slack.

The tick-level outbound poster for the headless ask-loop. A headless
``needs_user_input`` STOP parks a ``DeferredQuestion`` with no ``slack_ts``
(the SDK lane has no human at the harness), and ``task_repair._escalate_stall``
records the same shape — nobody posts either today. This scanner runs in the
global dispatch set (once per tick in the orchestrator loop, which has a
working backend) and posts each un-mirrored row via
:func:`teatree.core.notify_question_drains.drain_unmirrored_deferred_questions`,
stamping the delivered Slack coordinates so the reply scanner can later bind a reply.

It is the cross-tick peer of :class:`UndeliveredNotifyScanner`: same
durable-row-then-drain shape, different durability trigger (no-mirror at
ask time vs. no-backend at post time). It emits a :class:`ScanSignal` only
when it actually mirrors something.
"""

import logging
from dataclasses import dataclass

from django.db import OperationalError, ProgrammingError

from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeferredQuestionPosterScanner:
    overlay: str = ""
    name: str = "deferred_question_poster"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.notify_question_drains import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
            drain_unmirrored_deferred_questions,
        )

        try:
            mirrored, total = drain_unmirrored_deferred_questions(overlay=self.overlay)
        except (OperationalError, ProgrammingError):
            logger.info("DeferredQuestionPosterScanner: DeferredQuestion unavailable (DB not migrated yet) — skipping")
            return []
        except Exception:
            logger.exception("DeferredQuestionPosterScanner drain failed")
            return []
        if mirrored == 0:
            return []
        return [
            ScanSignal(
                kind="deferred_question.mirrored",
                summary=f"posted {mirrored}/{total} pending question(s) to Slack",
                payload={"mirrored": mirrored, "total": total},
            ),
        ]
