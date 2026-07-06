"""Scanner that DMs the owner a digest of everything waiting on them (PR-21).

Once per tick the global dispatch set gathers the durable waiting-on-you lane
(:func:`teatree.core.waiting.gather_waiting`) and, when it is non-empty, posts a
monospace table (PR-18's :func:`~teatree.backends.slack.table_format.render_table_message`)
to the owner DM via :func:`~teatree.core.notify.notify_user`.

The digest is deduped on the entries' content hash: the ``BotPing`` idempotency
key is ``waiting_digest:<hash>``, so an unchanged lane never re-DMs, and a fresh
:class:`ScanSignal` is emitted only when a genuinely new digest posts (a changed
lane, or the first sighting) — the sibling convention of
:class:`~teatree.loop.scanners.undelivered_notify.UndeliveredNotifyScanner`.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import OperationalError, ProgrammingError

from teatree.backends.slack.table_format import render_table_message
from teatree.core.notify import NotifyKind, notify_user
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WaitingDigestScanner:
    overlay: str = ""
    backend: "MessagingBackend | None" = None
    user_id: str | None = None
    name: str = "waiting_digest"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: scanner module loads before django.setup()
        from teatree.core.waiting import format_age, gather_waiting  # noqa: PLC0415 — deferred: Django-backed reads

        try:
            entries = gather_waiting(self.overlay)
            if not entries:
                return []
            content_hash = _content_hash([(entry.kind, entry.ref) for entry in entries])
            key = f"waiting_digest:{content_hash}"
            already_posted = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).exists()
            rows = [[entry.kind, format_age(entry.age), entry.ref] for entry in entries]
            message = render_table_message(["Kind", "Age", "Waiting on you"], rows, title="Waiting on you")
            posted = notify_user(
                message.fence,
                kind=NotifyKind.INFO,
                idempotency_key=key,
                blocks=message.blocks,
                backend=self.backend,
                user_id=self.user_id,
            )
        except (OperationalError, ProgrammingError):
            logger.info("WaitingDigestScanner: waiting-lane tables unavailable (DB not migrated yet) — skipping")
            return []
        except Exception:
            logger.exception("WaitingDigestScanner failed")
            return []

        if already_posted or not posted:
            return []
        return [
            ScanSignal(
                kind="waiting.digest",
                summary=f"{len(entries)} item(s) waiting on you",
                payload={"count": len(entries)},
            ),
        ]


def _content_hash(pairs: list[tuple[str, str]]) -> str:
    """Stable short hash over the (kind, ref) pairs, order-independent."""
    joined = "\n".join(f"{kind}|{ref}" for kind, ref in sorted(pairs))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]
