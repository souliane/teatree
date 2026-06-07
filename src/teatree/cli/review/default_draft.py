"""Default-draft helpers for ``t3 review post-comment`` (#1207).

Two thin module-level helpers kept out of :mod:`teatree.cli.review` so
the GitLab-MR review mechanics module stays under the OOP/LOC ceiling
(``scripts/hooks/check_module_health.py``) after the #1207 default flip:

* :func:`check_live_post` — chokepoint that consumes the Slack-recorded
    :class:`~teatree.core.models.live_post_approval.LivePostApproval`
    when ``post_comment(..., live=True)`` is called; returns the
    user-facing refusal message on a miss.
* :func:`notify_draft_created` — fire-and-forget Slack DM with the
    GitLab draft link, emitted once per successful default-draft post.

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


def notify_draft_created(*, repo: str, mr: int, body: str, message: str) -> None:
    """DM the user when a default-draft ``post-comment`` lands (#1207).

    Fire-and-forget — never raises into the caller. The idempotency key
    pins the DM to a stable cross-process digest of the result message
    so a re-post of the same body on the same MR doesn't trigger a
    second notification within the ``BotPing`` ledger window. ``hashlib``
    (not the builtin ``hash``) is used because the builtin is salted by
    ``PYTHONHASHSEED`` — two CLI invocations would produce different
    keys for the same message and bypass the dedupe gate.
    """
    import hashlib  # noqa: PLC0415

    from teatree.core.notify import NotifyKind  # noqa: PLC0415
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415

    body_preview = body.strip().splitlines()[0][:200] if body.strip() else ""
    text = (
        f"Posted a draft comment on `{repo}!{mr}` (#1207 default-draft gate).\n\n"
        f"{message}\n\n"
        f"Body: {body_preview}\n\n"
        f"Publish:  `t3 review publish-draft-notes {repo} {mr}`\n"
        f"Discard:  `t3 review list-draft-notes {repo} {mr}` then "
        f"`t3 review delete-draft-note {repo} {mr} <id>`"
    )
    digest = hashlib.sha1(message.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    notify_with_fallback(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=f"post_comment_draft:{repo}!{mr}:{digest}",
    )
