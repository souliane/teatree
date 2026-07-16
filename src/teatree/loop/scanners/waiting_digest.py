"""Scanner that records an INTERNAL digest of everything waiting on the owner (PR-21).

Once per tick the global dispatch set gathers the durable waiting-on-you lane
(:func:`teatree.core.waiting.gather_waiting`) and, when it is non-empty, records a
monospace table (PR-18's :func:`~teatree.backends.slack.table_format.render_table_message`)
through :func:`~teatree.core.notify.notify_user` with an
:attr:`~teatree.core.modelkit.notify_policy.NotifyAudience.INTERNAL` audience — the owner's
allowlist classifies the waiting digest as internal, so it is logged (a terminal
``BotPing.LOGGED`` row) and surfaced on the local loop statusline, but NEVER DM'd.

The digest is deduped on the entries' content hash: the ``BotPing`` idempotency
key is ``waiting_digest:<hash>``, so an unchanged lane records once, and a fresh
:class:`ScanSignal` is emitted only for a genuinely new digest (a changed lane, or
the first sighting) — the sibling convention of
:class:`~teatree.loop.scanners.undelivered_notify.UndeliveredNotifyScanner`.
"""

import hashlib
import logging
from dataclasses import dataclass

from django.db import OperationalError, ProgrammingError

from teatree.backends.slack.table_format import render_table_message
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WaitingDigestScanner:
    overlay: str = ""
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
            already_recorded = BotPing.objects.filter(
                idempotency_key=key, status__in=[BotPing.Status.SENT, BotPing.Status.LOGGED]
            ).exists()
            rows = [[entry.kind, format_age(entry.age), entry.ref] for entry in entries]
            message = render_table_message(["Kind", "Age", "Waiting on you"], rows, title="Waiting on you")
            # INTERNAL: logged terminally (never DM'd). ``notify_user`` short-circuits
            # before backend resolution, so ``blocks``/``backend``/``user_id`` are unused.
            notify_user(
                message.fence,
                kind=NotifyKind.INFO,
                idempotency_key=key,
                audience=NotifyAudience.INTERNAL,
            )
        except (OperationalError, ProgrammingError):
            logger.info("WaitingDigestScanner: waiting-lane tables unavailable (DB not migrated yet) — skipping")
            return []
        except Exception:
            logger.exception("WaitingDigestScanner failed")
            return []

        if already_recorded:
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
