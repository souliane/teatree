"""Default-draft helpers for ``t3 review post-comment`` (#1207).

Two thin module-level helpers kept out of :mod:`teatree.cli.review` so
the GitLab-MR review mechanics module stays under the OOP/LOC ceiling
(``scripts/hooks/check_module_health.py``) after the #1207 default flip:

* :func:`check_live_post` — chokepoint that consumes the Slack-recorded
    :class:`~teatree.core.models.live_post_approval.LivePostApproval`
    when ``post_comment(..., live=True)`` is called; returns the
    user-facing refusal message on a miss.
* :func:`notify_draft_created` — fire-and-forget Slack DM with the
    clickable MR link, emitted once per MR (coalescing every default-draft
    comment on that MR into a single terse line, not one essay per comment).

The shape mirrors :mod:`teatree.cli.review.on_behalf` exactly: the
service method calls a thin module helper that owns the lazy ORM
import. Keeping these out of the service class keeps the per-class
method count under the OOP cap.
"""


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
    from teatree.core.gates.live_post_gate import LivePostBlockedError, require_live_post_approval  # noqa: PLC0415

    try:
        require_live_post_approval(mr_url=f"{repo}!{mr}")
    except LivePostBlockedError as blocked:
        return str(blocked)
    return ""


def notify_draft_created(*, repo: str, mr: int, mr_url: str) -> None:
    """DM the user ONCE PER MR when default-draft ``post-comment`` notes land (#1207).

    Fire-and-forget — never raises into the caller. The idempotency key is
    scoped to the MR (``post_comment_draft:{repo}!{mr}``) with no per-comment
    suffix, so every draft comment posted on the same MR coalesces into a
    single DM through the ``BotPing`` ledger's SENT-idempotency no-op — one
    terse line per MR, never one essay per comment.

    The body is exactly one line — ``Posted draft comments on
    [<repo>!<mr>](<mr_url>)``. ``maybe_linkify`` (applied by ``notify_user``)
    rewrites the ``[label](url)`` markdown into a clickable Slack
    ``<url|label>`` link, and the ``INFO`` kind supplies the
    ``:information_source:`` marker. No per-comment breakdown, no
    publish/discard instructions.
    """
    from teatree.core.notify import NotifyKind  # noqa: PLC0415
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415

    notify_with_fallback(
        f"Posted draft comments on [{repo}!{mr}]({mr_url})",
        kind=NotifyKind.INFO,
        idempotency_key=f"post_comment_draft:{repo}!{mr}",
    )
