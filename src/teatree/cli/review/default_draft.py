"""Default-draft helpers for ``t3 review post-comment`` (#1207).

Module-level helpers kept out of :mod:`teatree.cli.review` so the
GitLab-MR review mechanics module stays under the OOP/LOC ceiling
(``scripts/hooks/check_module_health.py``) after the #1207 default flip:

* :func:`check_live_post` — chokepoint that consumes the Slack-recorded
    :class:`~teatree.core.models.live_post_approval.LivePostApproval`
    when ``post_comment(..., live=True)`` is called; returns the
    user-facing refusal message on a miss.
* :func:`notify_draft_created` — fire-and-forget Slack DM with the
    clickable MR link, emitted once per MR *revision* (coalescing every
    default-draft comment against one head into a single terse line, not
    one essay per comment; a later round re-notifies).
* :func:`resolve_reviewed_head_sha` — best-effort reviewed HEAD SHA that
    keys :func:`notify_draft_created`'s per-revision coalescing.

The shape mirrors :mod:`teatree.cli.review.on_behalf` exactly: the
service method calls a thin module helper that owns the lazy ORM
import. Keeping these out of the service class keeps the per-class
method count under the OOP cap.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from teatree.cli.review.diff import fetch_diff_refs

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabAPI


def check_live_post(*, repo: str, mr: int) -> str:
    """Return a refusal message when ``post-comment --live`` lacks a Slack-recorded approval (#1207).

    Empty string ``""`` means the gate is satisfied (a fresh,
    unconsumed
    :class:`~teatree.core.models.live_post_approval.LivePostApproval`
    has been claimed single-use); a non-empty return is the user-facing
    error the caller short-circuits with as ``(message, 1)``.

    The #1207 live-post token gate is orthogonal to the on-behalf mode:
    the colleague-visible ``--live`` publish needs an explicit, single-use
    approval token regardless of mode. The one-step ``t3 review authorize``
    (#126) is what mints that token in the same command that records the
    on-behalf authorization, so a single user action satisfies both gates.
    """
    from teatree.core.gates.live_post_gate import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        LivePostBlockedError,
        require_live_post_approval,
    )

    try:
        require_live_post_approval(mr_url=f"{repo}!{mr}")
    except LivePostBlockedError as blocked:
        return str(blocked)
    return ""


def notify_draft_created(*, repo: str, mr: int, mr_url: str, reviewed_head_sha: str) -> None:
    """DM the user ONCE PER MR REVISION when default-draft ``post-comment`` notes land (#1207).

    Fire-and-forget — never raises into the caller. The idempotency key is
    scoped to the MR *and the reviewed head* —
    ``post_comment_draft:{repo}!{mr}:{discriminator}`` — so every draft comment
    posted against the same MR revision coalesces into a single DM through the
    ``BotPing`` ledger's SENT-idempotency no-op: one terse line per review
    pass, never one essay per comment.

    Keying on the revision (``reviewed_head_sha``), not the bare MR, is what
    lets a LATER review round re-notify. A SENT ``BotPing`` row is permanent
    (no TTL / purge), so a bare per-MR key would silently suppress every future
    round's DM once the first landed — the user would never hear that
    round-two drafts are waiting. When the head SHA can't be resolved (a
    transient API failure, or a non-GitLab surface) the discriminator degrades
    to the UTC day, so the key never collapses to that permanently-suppressing
    per-MR form and a next-day re-review still re-notifies.

    The body is exactly one line — ``Posted draft comments on
    [<repo>!<mr>](<mr_url>)``. ``maybe_linkify`` (applied by ``notify_user``)
    rewrites the ``[label](url)`` markdown into a clickable Slack
    ``<url|label>`` link, and the ``INFO`` kind supplies the
    ``:information_source:`` marker. No per-comment breakdown, no
    publish/discard instructions.
    """
    from teatree.core.notify import NotifyKind  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415 — deferred: keeps CLI startup light

    discriminator = reviewed_head_sha or datetime.now(tz=UTC).strftime("%Y-%m-%d")
    notify_with_fallback(
        f"Posted draft comments on [{repo}!{mr}]({mr_url})",
        kind=NotifyKind.INFO,
        idempotency_key=f"post_comment_draft:{repo}!{mr}:{discriminator}",
    )


def resolve_reviewed_head_sha(api: "GitLabAPI", repo: str, mr: int) -> str:
    """Best-effort reviewed HEAD SHA for :func:`notify_draft_created`'s discriminator.

    Reuses :func:`teatree.cli.review.diff.fetch_diff_refs` (a single MR GET)
    and returns its ``head_sha``. Returns ``""`` on any lookup failure — a
    transport/status error (``httpx.HTTPError``) or a malformed body
    (``ValueError`` from ``response.json()``) — so the caller degrades to the
    UTC-day discriminator. Never raises: the after-post DM is a courtesy
    receipt and a head-SHA hiccup must not break the already-successful draft
    post.
    """
    try:
        diff_refs, _ = fetch_diff_refs(api, repo.replace("/", "%2F"), mr)
    except (httpx.HTTPError, ValueError):
        return ""
    return diff_refs.get("head_sha", "") if diff_refs else ""
