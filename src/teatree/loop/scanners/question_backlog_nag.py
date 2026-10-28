"""Bump unanswered owner questions in the threads where replies bind."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import OperationalError, ProgrammingError

from teatree.core import notify_question_drains
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
        try:
            bumped, pending = notify_question_drains.reask_escalated_questions(
                overlay=self.overlay, backend=self.backend, user_id=self.user_id
            )
        except (OperationalError, ProgrammingError):
            logger.info("QuestionBacklogNagScanner: DeferredQuestion unavailable (DB not migrated yet) — skipping")
            return []
        if not bumped:
            return []
        return [
            ScanSignal(
                kind="deferred_question.resurfaced",
                summary=f"{bumped} unanswered question(s) bumped in their own threads",
                payload={"pending": pending, "bumped": bumped},
            ),
        ]
