"""Loop-edge substrate-hold pinger (orchestration layer).

A held SUBSTRATE merge is an INTERNAL notification — logged, never DM'd — routed
through the ``notify_with_fallback`` egress so the deny-by-default notify policy
records it without spamming the owner (a hold that stays held re-fires every
tick, so a DM would recur stale for hours, #F3). Lives at the ``teatree.loop``
orchestration layer — NOT in ``teatree.loop.scanners`` (domain), where the tach
boundary forbids importing ``teatree.messaging`` / ``teatree.core.notify``
(integration). The scanner depends only on the ``SubstratePinger`` Protocol; this
concrete notify egress is injected at the loop edge, mirroring ``domain_jobs``'s use.
"""

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind
from teatree.messaging import notify_with_fallback


class NotifyWithFallbackSubstratePinger:
    """Production :class:`~teatree.loop.scanners.pr_sweep.SubstratePinger` (ping-and-hold).

    The substrate-hold signal is INTERNAL (log-only) — recorded through the
    BotPing ledger under the per-diff idempotency key but never DM'd, so a hold
    that stays held can never surface a stale merge DM to the owner.
    """

    def ping(self, *, text: str, idempotency_key: str) -> None:  # noqa: PLR6301 — instance method satisfies the injected SubstratePinger Protocol.
        # One kwarg per line, deliberately: gitleaks' generic-api-key rule keys off
        # a ``…key=`` prefix and then captures the following high-entropy-looking
        # text. On the one-line call it ran the capture on past ``idempotency_key=``
        # onto ``audience=NotifyAudience.INTERNAL`` (an enum reference, no secret) and
        # false-tripped the repo's own secret gate (#3344). Breaking each kwarg onto
        # its own line stops the capture running across kwargs.
        notify_with_fallback(
            text=text,
            kind=NotifyKind.INFO,
            idempotency_key=idempotency_key,
            audience=NotifyAudience.INTERNAL,
        )
