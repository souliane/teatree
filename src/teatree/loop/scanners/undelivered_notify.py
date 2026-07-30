"""Scanner that re-delivers INFO DMs stranded with no backend (#173).

A bot→user INFO DM fired from a sub-agent shell whose restricted PATH
cannot read ``pass`` resolves no messaging backend, so ``notify_user``
parks a recoverable NOOP :class:`BotPing` row instead of losing the DM.
This scanner runs in the global dispatch set — once per tick in the
orchestrator loop, which *does* have a working backend — and re-attempts
each parked row via :func:`teatree.core.notify.drain_undelivered_notifies`.

It is the cross-tick peer of the away→present
:func:`drain_deferred_questions` drain: same durable-row-then-drain shape,
different durability trigger (no-backend at post time vs. away-mode at
ask time). It emits no actionable :class:`ScanSignal` — like the
``IncomingEvents`` drain it is a side-effecting consumer, surfaced on the
statusline only when it actually re-delivers something.
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
class UndeliveredNotifyScanner:
    limit: int = 50
    # An explicit messaging backend + user id so the GLOBAL dispatch tick can
    # re-deliver a stranded owner-audience DM even with no ``T3_OVERLAY_NAME`` set;
    # ``None``/empty falls back to ``notify_user``'s overlay resolution.
    backend: "MessagingBackend | None" = None
    user_id: str = ""
    name: str = "undelivered_notify"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.notify import drain_undelivered_notifies  # noqa: PLC0415 — deferred: loaded at tick time

        try:
            delivered, total = drain_undelivered_notifies(limit=self.limit, backend=self.backend, user_id=self.user_id)
        except (OperationalError, ProgrammingError):
            logger.info("UndeliveredNotifyScanner: BotPing unavailable (DB not migrated yet) — skipping")
            return []
        except Exception:
            logger.exception("UndeliveredNotifyScanner drain failed")
            return []
        if delivered == 0:
            return []
        return [
            ScanSignal(
                kind="notify.redelivered",
                summary=f"re-delivered {delivered}/{total} stranded bot→user DM(s)",
                payload={"delivered": delivered, "total": total},
            ),
        ]
