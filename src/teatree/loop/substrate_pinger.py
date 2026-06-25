"""Loop-edge substrate-hold pinger (orchestration layer).

A held SUBSTRATE merge DMs the owner once (ping-and-hold) via the existing
``notify_with_fallback`` egress. Lives at the ``teatree.loop`` orchestration
layer — NOT in ``teatree.loop.scanners`` (domain), where the tach boundary
forbids importing ``teatree.messaging`` / ``teatree.notify`` (integration). The
scanner depends only on the ``SubstratePinger`` Protocol; this concrete notify
egress is injected at the loop edge, mirroring ``domain_jobs``'s use.
"""

from teatree.messaging import notify_with_fallback
from teatree.notify import NotifyKind


class NotifyWithFallbackSubstratePinger:
    """Production :class:`~teatree.loop.scanners.pr_sweep.SubstratePinger` (ping-and-hold).

    The per-diff idempotency key dedupes across re-ticks through the BotPing
    ledger, so the owner is pinged exactly once per held substrate diff.
    """

    def ping(self, *, text: str, idempotency_key: str) -> None:  # noqa: PLR6301 — instance method satisfies the injected SubstratePinger Protocol.
        notify_with_fallback(text=text, kind=NotifyKind.INFO, idempotency_key=idempotency_key)
