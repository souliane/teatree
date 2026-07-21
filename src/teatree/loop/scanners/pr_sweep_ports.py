"""Adapter port Protocols the PR-sweep scanner depends on (#1248).

The sweep scanner (:class:`teatree.loop.scanners.pr_sweep.PrSweepScanner`)
is injected with four adapters — the ``gh`` PR API, the merge keystone,
the review dispatcher, and the merge notifier. Their *interface* contracts
(the ports) live here; the production *implementations* live in the sibling
:mod:`teatree.loop.scanners.pr_sweep_adapters`. Splitting the ports out of
the scanner file keeps the scanner focused on the decision ladder and gives
the adapters one canonical Protocol to satisfy.
"""

from typing import Protocol, runtime_checkable

from teatree.loop.scanners.pr_sweep_types import PrSummary

__all__ = [
    "MergeKeystone",
    "MergeNotifier",
    "PrApiClient",
    "ReviewDispatcher",
]


@runtime_checkable
class PrApiClient(Protocol):
    """Adapter over ``gh`` used by the scanner — mockable in tests.

    Two methods only: list open PRs on a repo, and fetch the per-PR
    detail block (head SHA, draft, reviews, checks). The implementation
    shells out to ``gh`` with an optional ``GH_TOKEN`` override so each
    overlay can hit its private repos under its own PAT.
    """

    def list_open_prs(self, *, slug: str) -> list[PrSummary]: ...  # pragma: no branch

    def main_check_failed(self, *, slug: str, check_name: str) -> bool: ...  # pragma: no branch

    def merge_pr_squash_bound(
        self,
        *,
        slug: str,
        pr_id: int,
        expected_head_oid: str,
    ) -> tuple[bool, str]: ...  # pragma: no branch


@runtime_checkable
class MergeKeystone(Protocol):
    """Adapter over ``call_command('ticket', 'merge', ...)`` — mockable."""

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        """Return ``(merged, merged_sha, error, escalation_kind, standing_delegation_by)``.

        ``error`` is the rejection reason; ``escalation_kind`` is ``"substrate"``
        when the refusal is a substrate sign-off hold (else empty) so the loop
        pings the owner ONLY on substrate. ``human_authorized`` is the standing
        substrate authorizer the sweep sources from
        ``substrate_auto_merge_authorized_by`` and re-presents at merge time (#3413)
        — empty for the legacy hold-for-owner behaviour. ``standing_delegation_by``
        echoes back the config-sourced authorizer id when the keystone actually
        authorized the merge via that standing delegation (empty otherwise), so the
        sweep posts the "informed, not asked" notification only on such a merge.
        """
        ...  # pragma: no branch


@runtime_checkable
class ReviewDispatcher(Protocol):
    """Enqueue ONE claimable reviewing task for a no-review own PR (#68) — mockable.

    The production adapter records an
    :class:`teatree.core.models.auto_review_dispatch.AutoReviewDispatch` row
    (deduped per ``(slug, pr_id, head_sha)``) and creates the
    ``Task(phase=reviewing)`` the loop self-pump dispatches to ``t3:reviewer``.
    Returns ``True`` when a new task was armed, ``False`` when a task for this
    head already exists (the dedup no-op).
    """

    def enqueue(
        self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str
    ) -> bool: ...  # pragma: no branch


@runtime_checkable
class MergeNotifier(Protocol):
    """Post a Slack DM on an actual merge, and on a flag-level signal.

    ``announce`` is the merge acceptance gate (a DM only when a merge
    lands). ``flag`` is the optional Slack mirror for a flag-level signal
    the scanner refuses to act on autonomously — a conflicted open PR, or
    a green solo-overlay PR with no recorded independent cold-review. The
    statusline always carries the flag; the Slack DM is the optional
    escalation rung, mirroring the ``forgotten_merge`` detector ladder.
    """

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None: ...  # pragma: no branch

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None: ...  # pragma: no branch
