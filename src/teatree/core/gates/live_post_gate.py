"""Recorded-approval orchestration for the live-post pre-gate (#1207).

``t3 review post-comment`` defaults to creating a draft note. The
``--live`` flag asks the CLI to publish a colleague-visible inline /
general comment directly — a path that must never fire without an
explicit Slack-DM authorization from the user. The satisfier is a
:class:`~teatree.core.models.live_post_approval.LivePostApproval` row
minted by ``t3 review approve-live-post`` after the Slack DM at the
provided ``--slack-ts`` is verified (from the user, recent, contains
an explicit approval phrase).

:func:`require_live_post_approval` is the chokepoint helper every
``post-comment --live`` invocation calls *before* it publishes:

* matching unconsumed approval → consume it single-use, return; the
    live post proceeds;
* no recorded approval (or stale / wrong MR / already consumed) →
    raise :class:`LivePostBlockedError`. The caller must NOT publish;
    it surfaces the refusal with the exact ``approve-live-post``
    invocation the user records to satisfy the gate.

The ORM-model import lives inside the function body (not at module
top) so importing the gate helper from the CLI layer does not require
``django.setup()`` to have run — mirrors
:mod:`teatree.core.on_behalf_gate_recorded`.
"""

APPROVE_LIVE_POST_USAGE = (
    "    t3 review approve-live-post <mr-url> --slack-ts <ts>\n"
    "where <ts> is the Slack timestamp of the user's DM authorising the "
    "live post (must be recent, from the user, contain an explicit "
    "approval phrase such as 'post live' / 'submit it' / 'go ahead')."
)

# The two paths that reach a reviewer WITHOUT an approval token. Both already
# exist; neither was named in the refusal, so an agent whose comment is a review
# finding on the owner's OWN MR read "record a Slack approval" as the only way
# forward and stalled. Naming them costs nothing and relaxes nothing: the live,
# colleague-visible post stays gated exactly as before.
UNGATED_ALTERNATIVES = (
    "Two paths need no approval token and may already be what you want:\n"
    "  * drop --live — the default is a DRAFT note, colleague-invisible until "
    "the user submits it, so a review finding can be recorded right now;\n"
    "  * `t3 review reply-to-discussion <repo> <mr> <discussion-id> <body>` — on "
    "an MR the OWNER AUTHORED, an author-side reply to an existing thread is the "
    "owner's own voice on the owner's own work and carries the #1207/#960 "
    "author-side carve-out (authorship is PROVED per call and fails closed). Use "
    "it when the comment answers or amends a review thread on your own MR."
)


class LivePostBlockedError(RuntimeError):
    """No Slack-recorded approval for this MR — the ``--live`` post must NOT publish.

    Carries the ``mr_url`` plus a user-facing message that names the
    exact ``t3 review approve-live-post`` invocation the user needs to
    record, so the refusal can be surfaced verbatim.
    """

    def __init__(self, mr_url: str) -> None:
        self.mr_url = mr_url
        super().__init__(
            f"live post blocked (#1207): no Slack-recorded approval "
            f"for {mr_url!r}. The default of `t3 review post-comment` "
            f"is a DRAFT (safe). To post a live, colleague-visible "
            f"comment, the user records a one-shot approval token by "
            f"DM'ing approval and then running:\n"
            f"{APPROVE_LIVE_POST_USAGE}\n"
            f"{UNGATED_ALTERNATIVES}"
        )


def require_live_post_approval(*, mr_url: str) -> None:
    """Gate one ``--live`` post-comment against the Slack-recorded approval.

    Consumes a matching unconsumed, fresh approval (single-use,
    MR-URL-scoped, within the
    :data:`~teatree.core.models.live_post_approval.LIVE_POST_APPROVAL_TTL_MINUTES`
    TTL window) or raises :class:`LivePostBlockedError` — never a silent
    drop, never an unattended live publish.
    """
    from teatree.core.models.live_post_approval import LivePostApproval  # noqa: PLC0415 — deferred: ORM/app-registry

    consumed = LivePostApproval.consume(mr_url=mr_url)
    if consumed is None:
        raise LivePostBlockedError(mr_url)
