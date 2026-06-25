"""Substrate-hold ping seam for the PR sweep (ping-and-hold, #3.1).

A held SUBSTRATE merge DMs the owner once via the injected
:class:`SubstratePinger` (the concrete ``notify_with_fallback`` egress is wired
at the loop edge — this domain module stays import-clean of messaging/notify).
The per-diff idempotency key keeps it to one ping per held diff and re-fires
only on a new reviewed SHA.
"""

import logging
from typing import Protocol, runtime_checkable

from teatree.loop.scanners.pr_sweep_types import PrSummary

logger = logging.getLogger(__name__)


@runtime_checkable
class SubstratePinger(Protocol):
    """Deliver a bot→user DM when a substrate merge is held (ping-and-hold) — mockable.

    The production adapter wraps :func:`teatree.messaging.notify_with_fallback`,
    so the per-diff ``idempotency_key`` dedupes across re-ticks via the
    :class:`teatree.core.models.BotPing` ledger — exactly one ping per held
    substrate diff, re-firing only on a new reviewed SHA.
    """

    def ping(self, *, text: str, idempotency_key: str) -> None: ...  # pragma: no branch


def substrate_hold_text(pr: PrSummary, *, reviewed_sha: str, error: str) -> str:
    """The owner-facing DM body for a held substrate merge."""
    return (
        f"substrate merge held for your sign-off: [{pr.slug}#{pr.number}]({pr.url}) "
        f"@ {reviewed_sha[:8]} — {error or 'substrate change, held for the owner'}"
    )


def substrate_hold_key(pr: PrSummary, *, reviewed_sha: str) -> str:
    """The per-diff idempotency key so the BotPing ledger dedupes re-ticks."""
    return f"substrate-hold:{pr.slug}#{pr.number}:{reviewed_sha}"


def ping_substrate_hold(pinger: "SubstratePinger | None", *, pr: PrSummary, reviewed_sha: str, error: str) -> None:
    """DM the owner ONCE that a substrate merge is held (ping-and-hold).

    No-op when no pinger is wired. Best-effort: a pinger error never aborts the
    sweep — the substrate clear is still held; only the DM is missed.
    """
    if pinger is None:
        return
    try:
        pinger.ping(
            text=substrate_hold_text(pr, reviewed_sha=reviewed_sha, error=error),
            idempotency_key=substrate_hold_key(pr, reviewed_sha=reviewed_sha),
        )
    except Exception:
        logger.exception("pr_sweep failed to ping substrate-hold for %s#%d", pr.slug, pr.number)
