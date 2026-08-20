"""Scanner that re-raises the unanswered ``DeferredQuestion`` backlog on a cadence.

The mechanism to resurface a question backlog existed (``t3 teatree questions
resurface`` → :func:`teatree.core.notify_question_drains.drain_deferred_questions`)
but nothing called it, and its per-question idempotency key made it single-shot
anyway — so a question the owner never answered was posted once and then sat.
This scanner is the caller: it runs in the global dispatch set (once per tick in
the orchestrator loop, which has a working backend) and drives
:func:`teatree.core.notify_question_drains.resurface_question_backlog`, whose
interval bucket keeps the message count to one digest per day.

It is the recurring peer of :class:`DeferredQuestionPosterScanner`: that one is
the FIRST post of a never-mirrored row, this one is the nag for a row already
posted and still unanswered. Directive #36 asks for the nag in the same Slack
thread rather than a new post, which is what the digest-into-the-active-DM-thread
shape gives.

The nag has TWO halves and the scanner drives both, because the digest alone is
a re-ask nobody can answer: a reply under it carries the digest's thread ts, so
the exact join in :mod:`teatree.loop.question_binding` matches no question, and
at a backlog above one the sole-live-question rung refuses to guess. So
:func:`~teatree.core.notify_question_drains.reask_escalated_questions` bumps the
few most urgent rows INSIDE their own mirror threads — where a bare reply binds —
and the digest shrinks to the count that frames them. The re-ask runs even when
the digest is deduped inside its bucket: the two share the bucket, not the ping,
so a digest already delivered this window must not suppress the bumps.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import OperationalError, ProgrammingError

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QuestionBacklogNagScanner:
    overlay: str = ""
    # Explicit backend + user id for the same reason the poster takes them: the
    # GLOBAL dispatch tick runs with no ``T3_OVERLAY_NAME`` and would otherwise
    # no-op on an unresolved overlay backend.
    backend: "MessagingBackend | None" = None
    user_id: str = ""
    name: str = "question_backlog_nag"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.notify_question_drains import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
            reask_escalated_questions,
            resurface_question_backlog,
        )

        try:
            posted, pending = resurface_question_backlog(
                overlay=self.overlay, backend=self.backend, user_id=self.user_id
            )
            bumped, _mirrored = reask_escalated_questions(
                overlay=self.overlay, backend=self.backend, user_id=self.user_id
            )
        except (OperationalError, ProgrammingError):
            logger.info("QuestionBacklogNagScanner: DeferredQuestion unavailable (DB not migrated yet) — skipping")
            return []
        except Exception:
            logger.exception("QuestionBacklogNagScanner resurface failed")
            return []
        if not posted and not bumped:
            return []
        return [
            ScanSignal(
                kind="deferred_question.resurfaced",
                summary=f"{pending} unanswered question(s): one digest, {bumped} bumped in their own threads",
                payload={"pending": pending, "bumped": bumped, "digest": posted},
            ),
        ]
