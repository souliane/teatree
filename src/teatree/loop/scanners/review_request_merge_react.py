""":merge: reaction on a merged review-request's Slack message (#1797).

When the agent posts a review-request to Slack it tracks the message in a
``ReviewRequestPost`` row (channel + thread ts). When the requested MR
later merges, a ``:merge:`` reaction on that original message closes the
loop visually — reviewers see at a glance which requests have landed,
instead of merged requests looking still-open in the channel.

This scanner is the merge-audit half of the review-request ledger,
distinct from the fibonacci nag (:mod:`teatree.loop.scanners.review_nag`):
the nag is noise the user opts into, the merge reaction is a low-noise
positive signal that always runs. Both walk the same ``ReviewRequestPost``
rows and both mark a row ``done_at`` when its MR leaves the open state, so
whichever fires first stops the other from acting on the same row.

Token routing follows the #1750 rule via
:meth:`MessagingBackend.react_routed` — reacting follows the same posting
rules as messaging: a reaction on a colleague/channel message goes out
under the personal ``xoxp`` token, a reaction on the agent's own DM goes
out under the bot. A personal token that lacks ``reactions:write`` returns
``missing_scope``; the scanner surfaces that as a signal and closes the
row (the reaction can never land, so retrying it every tick is pure
churn) rather than crashing the tick.

Idempotency: ``done_at`` is the claim. It is set with an atomic
conditional ``UPDATE`` *before* the Slack call, so two concurrent ticks
cannot both react — the tick that loses the claim matches zero rows and
skips. A transient Slack failure (``not_in_channel``, a raised transport
error) releases the claim so a future tick retries; a definitive
``missing_scope`` keeps the row closed.

Self-authored skip (#1838): the bot must never react on a review-request
the *user themselves* posted for their *own* MR. Before claiming the row
the merge author is fetched from the code host and matched against the
user's forge identities via :func:`teatree.core.review.review_candidate.author_is_self`
— the same notion of "self" the review-candidate skip-conditions use.
A self-authored merged MR closes the row (so the nag train stops too)
without reacting; an unresolved author is not provably self, so it is
left for a later tick rather than risk reacting on the user's own MR.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.utils import timezone

from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend, PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.core.review.review_candidate import author_is_self
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

MERGE_REACTION_EMOJI = "merge"

_MISSING_SCOPE_ERRORS = frozenset({"missing_scope", "no_permission"})
_REACTION_PRESENT_ERRORS = frozenset({"already_reacted"})


def _claim_post(post: ReviewRequestPost) -> bool:
    updated = ReviewRequestPost.objects.filter(pk=post.pk, done_at__isnull=True).update(done_at=timezone.now())
    return updated == 1


def _resolve_self_identities(host: CodeHostBackend | None, identities: Iterable[str]) -> set[str]:
    """Union of configured identity aliases and the host's current user.

    Configured ``identities`` come from ``user_identity_aliases``; the
    host's ``current_user`` is folded in so the self-author skip still
    works when no aliases are configured (legacy single-identity setups).
    A failed ``current_user`` lookup is non-fatal — the configured aliases
    still apply.
    """
    resolved = {name for name in identities if name}
    if host is not None:
        try:
            current = host.current_user()
        except Exception as exc:  # noqa: BLE001 — a current-user lookup must never crash a tick.
            logger.warning("review_request_merge_react: current_user lookup failed: %s", exc)
            current = ""
        if current:
            resolved.add(current)
    return resolved


def _is_self_authored(post: ReviewRequestPost, host: CodeHostBackend | None, identities: Iterable[str]) -> bool | None:
    """Classify authorship of the MR for *post* as self / colleague / unresolved.

    Returns:
    * ``True`` — the author RESOLVED to one of the user's identities. The
        reaction is skipped and the row is closed (self-authored).
    * ``False`` — the author resolved to someone else (or there is no
        self-identity to protect). The colleague path proceeds and the row
        may be reacted on.
    * ``None`` — the author lookup FAILED (the backend raised, or returned
        ``""`` for a transient failure / unparsable URL). This is a *transient*
        outcome, not a verdict: the caller skips this tick WITHOUT stamping
        ``done_at`` so a later tick retries (F5.2). Permanently closing the row
        here would abandon a colleague's merged review-request on one bad forge
        read.
    """
    self_identities = _resolve_self_identities(host, identities)
    if not self_identities or host is None:
        return False
    try:
        author = host.get_pr_author(pr_url=post.mr_url)
    except Exception as exc:  # noqa: BLE001 — author lookup must never crash a tick.
        logger.warning("review_request_merge_react: author lookup failed for %s: %s", post.mr_url, exc)
        return None
    if not author:
        return None
    return author_is_self(author, current_user="", self_identities=self_identities)


def _release_post(post: ReviewRequestPost) -> None:
    ReviewRequestPost.objects.filter(pk=post.pk).update(done_at=None)


def _signal_for_react_response(post: ReviewRequestPost, response: RawAPIDict) -> ScanSignal:
    error = str(response.get("error") or "")
    if response.get("ok") or error in _REACTION_PRESENT_ERRORS:
        return ScanSignal(
            kind="review_request_merge_react.reacted",
            summary=f"Reacted :merge: on review-request for {post.mr_url}",
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )
    if error in _MISSING_SCOPE_ERRORS:
        needed = str(response.get("needed") or "reactions:write")
        return ScanSignal(
            kind="review_request_merge_react.missing_scope",
            summary=(
                f":merge: reaction for {post.mr_url} skipped — the personal xoxp token "
                f"is missing the {needed!r} scope. Re-run `t3 setup slack-user-token`."
            ),
            payload={"mr_url": post.mr_url, "needed": needed, "post_id": post.pk},
        )
    _release_post(post)
    return ScanSignal(
        kind="review_request_merge_react.react_failed",
        summary=f"Slack :merge: reaction refused for {post.mr_url}: {error or 'unknown_error'}",
        payload={"mr_url": post.mr_url, "error": error or "unknown_error", "post_id": post.pk},
    )


def _close_self_authored(post: ReviewRequestPost) -> ScanSignal:
    """Close *post* without reacting — the user authored their own MR (#1838)."""
    ReviewRequestPost.objects.filter(pk=post.pk, done_at__isnull=True).update(done_at=timezone.now())
    return ScanSignal(
        kind="review_request_merge_react.self_authored",
        summary=f":merge: reaction skipped for {post.mr_url} — the user authored this MR",
        payload={"mr_url": post.mr_url, "post_id": post.pk},
    )


def react_merge_on_post(
    post: ReviewRequestPost,
    messaging: MessagingBackend,
    *,
    host: CodeHostBackend | None = None,
    identities: Iterable[str] = (),
) -> ScanSignal | None:
    """Atomically claim *post* and react ``:merge:`` on its tracked Slack message.

    The single entry point shared by the merge-react scanner and the nag
    scanner's merged branch so a merge discovered by either path reacts
    exactly once. The conditional ``done_at`` claim is the idempotency lock:
    a row already claimed by another tick (or the sibling scanner) matches
    zero rows and returns ``None`` without reacting. A transient Slack
    failure releases the claim for a later retry; ``missing_scope`` keeps
    the row closed. Returns ``None`` when there is no thread to react on or
    the claim is lost.

    Self-authored skip (#1838): when the MR was authored by the user
    themselves (matched against ``identities`` plus ``host.current_user()``),
    the row is closed and a ``self_authored`` signal returned WITHOUT
    reacting — the bot must never react on the user's own review-request.

    A *failed* author lookup (F5.2) is transient, not a verdict: the tick is
    skipped WITHOUT stamping ``done_at`` (returning ``None``) so a later tick
    retries, rather than permanently closing a colleague's merged
    review-request on one bad forge read.
    """
    if not post.slack_thread_ts:
        return None
    self_authored = _is_self_authored(post, host, identities)
    if self_authored is None:
        logger.debug(
            "review_request_merge_react: author lookup unresolved for %s — skipping tick without stamping",
            post.mr_url,
        )
        return None
    if self_authored:
        return _close_self_authored(post)
    return _claim_and_react(post, messaging)


def _claim_and_react(post: ReviewRequestPost, messaging: MessagingBackend) -> ScanSignal | None:
    """Claim *post* and post the ``:merge:`` reaction, releasing the claim on failure.

    Split out of :func:`react_merge_on_post` so the self-authored / transient
    guards and the claim+react mechanics each stay within the return-count
    budget. A lost claim returns ``None``; a gated / transient failure releases
    the claim and returns the corresponding signal for retry.
    """
    if not _claim_post(post):
        return None
    try:
        response = OnBehalfSlackEgress(messaging).react(
            channel=post.slack_channel_id,
            ts=post.slack_thread_ts,
            emoji=MERGE_REACTION_EMOJI,
            target=post.mr_url,
            action="merge_reaction",
            destination=f"review-request for {post.mr_url}",
            artifact_url=post.mr_url,
            summary=":merge: on merged review-request",
        )
    except OnBehalfPostBlockedError as blocked:
        _release_post(post)
        return ScanSignal(
            kind="review_request_merge_react.gated",
            summary=str(blocked),
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )
    except Exception as exc:  # noqa: BLE001 — a Slack transport error must not crash the tick.
        _release_post(post)
        logger.warning(
            "review_request_merge_react: react failed for %s on %s/%s: %s",
            post.mr_url,
            post.slack_channel_id,
            post.slack_thread_ts,
            exc,
        )
        return ScanSignal(
            kind="review_request_merge_react.react_failed",
            summary=f"Slack :merge: reaction failed for {post.mr_url}: {exc}",
            payload={"mr_url": post.mr_url, "error": str(exc), "post_id": post.pk},
        )
    return _signal_for_react_response(post, response)


@dataclass(slots=True)
class ReviewRequestMergeReactScanner:
    """React ``:merge:`` on the review-request message once its MR merges (#1797).

    Walks open ``ReviewRequestPost`` rows; for each merged MR it reacts on
    the tracked Slack message and marks the row done so the reaction lands
    exactly once. Stateless beyond the rows it walks — safe to run every
    tick regardless of the nag flag.
    """

    messaging: MessagingBackend | None
    host: CodeHostBackend | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)
    name: str = "review_request_merge_react"

    def scan(self) -> list[ScanSignal]:
        messaging = self.messaging
        host = self.host
        if messaging is None or host is None:
            return []
        signals: list[ScanSignal] = []
        for post in ReviewRequestPost.objects.filter(done_at__isnull=True).order_by("created_at"):
            signal = self._process_one(post, messaging, host)
            if signal is not None:
                signals.append(signal)
        return signals

    def _process_one(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        host: CodeHostBackend,
    ) -> ScanSignal | None:
        if not post.slack_thread_ts:
            return None
        if self._open_state(host, post.mr_url) is not PrOpenState.MERGED:
            return None
        return react_merge_on_post(post, messaging, host=host, identities=self.identities)

    @staticmethod
    def _open_state(host: CodeHostBackend, mr_url: str) -> PrOpenState:
        """Open-state lookup that never crashes a tick — failures map to ``UNKNOWN``."""
        try:
            return host.get_pr_open_state(pr_url=mr_url)
        except Exception as exc:  # noqa: BLE001 — backend lookup must never crash a tick.
            logger.warning("review_request_merge_react: open-state lookup failed for %s: %s", mr_url, exc)
            return PrOpenState.UNKNOWN
