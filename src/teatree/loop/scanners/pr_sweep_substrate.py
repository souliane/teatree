"""Substrate-hold ping seam for the PR sweep (ping-and-hold, #3.1).

A held SUBSTRATE merge DMs the owner once via the injected
:class:`SubstratePinger` (the concrete ``notify_with_fallback`` egress is wired
at the loop edge — this domain module stays import-clean of messaging/notify).
The per-diff idempotency key keeps it to one ping per held diff and re-fires
only on a new reviewed SHA.
"""

import logging
from typing import Protocol, runtime_checkable

from teatree.core.merge.ci_rollup import fetch_pr_changed_paths
from teatree.core.models.merge_clear import diff_paths_are_substrate
from teatree.loop.scanners.pr_sweep_types import MergeAttempt, PrSummary

logger = logging.getLogger(__name__)


def pr_diff_is_substrate(pr: PrSummary) -> bool:
    """True iff *pr*'s changed paths make it a substrate change — FAIL-SAFE (Finding 2).

    The solo-overlay no-CLEAR bypass has no CLEAR row to read ``is_substrate`` from,
    so the diff is classified live via :func:`fetch_pr_changed_paths` +
    :func:`diff_paths_are_substrate`. The fetch is FAIL-SAFE: an exception OR an
    empty list (a real open PR always changes >=1 file, so an empty list signals the
    forge fetch failed) is treated conservatively as substrate so the can't-tell case
    HOLDS rather than widening to a silent merge. Only a non-empty, confirmed-non-
    substrate diff lets the merge proceed.
    """
    try:
        paths = fetch_pr_changed_paths(pr.slug, pr.number)
    except Exception:
        logger.exception("pr_sweep changed-paths fetch failed for %s#%d — holding", pr.slug, pr.number)
        return True
    if not paths:
        logger.warning("pr_sweep empty changed-paths for %s#%d — holding conservatively", pr.slug, pr.number)
        return True
    return diff_paths_are_substrate(paths)


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


def hold_solo_overlay_substrate(pinger: "SubstratePinger | None", *, pr: PrSummary) -> MergeAttempt:
    """Ping-and-hold a substrate PR on the solo-overlay no-CLEAR bypass (Finding 2).

    Reuses the existing substrate pinger + per-diff idempotency key (keyed on the
    live head SHA, since there is no CLEAR ``reviewed_sha`` here) so a re-tick dedupes
    through the BotPing ledger. The PR is held, never raw-merged.
    """
    ping_substrate_hold(
        pinger,
        pr=pr,
        reviewed_sha=pr.head_sha,
        error="substrate change on solo overlay, held for the owner",
    )
    return MergeAttempt(
        slug=pr.slug,
        pr_id=pr.number,
        decision="blocked",
        reason="solo_overlay_substrate_hold",
        url=pr.url,
    )
