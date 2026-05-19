"""Recorded-approval orchestration for the on-behalf pre-gate (#960/#961).

``teatree.on_behalf_gate`` holds the pure setting resolver
(``resolve_on_behalf_verdict``) — it depends only on
``teatree.config`` and stays in that thin layer. The *satisfiable*
channel needs the :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
/ :class:`~teatree.core.models.on_behalf_approval.OnBehalfAudit` ORM models,
so its orchestration lives here in ``teatree.core`` (which legitimately
depends on both ``teatree.on_behalf_gate`` and ``teatree.core.models``),
exactly as #953 split ``teatree.utils.approval`` (pure) from
``teatree.core.db_approval_gate`` (ORM-backed).

:func:`require_on_behalf_approval` is the single chokepoint helper every
on-behalf publish path calls *before* it publishes. Its outcome depends
on the tri-state :class:`~teatree.config.OnBehalfPostMode`:

*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.PROCEED` (mode
    :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE`) → return, the post
    proceeds;
*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.AUTO_DRAFT`
    (:attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` + the action is
    a draft-form post like ``post_draft_note``) → emit a fire-and-forget
    bot→user DM and return; the post proceeds without consuming any
    recorded approval. The audit lives on the ``BotPing`` ledger
    (``notify_user``); no ``OnBehalfAudit`` row is written because no
    approval was needed;
*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.BLOCK`
    (:attr:`~teatree.config.OnBehalfPostMode.ASK` or
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` for non-draft
    actions) + a recorded, unconsumed, exactly-scoped
    :class:`OnBehalfApproval` → consume it single-use, write an
    :class:`OnBehalfAudit` row, return — the post proceeds;
*   BLOCK + no recorded approval → raise :class:`OnBehalfPostBlockedError`.
    The caller must NOT publish; it surfaces the blocked post to the user
    (the user-notify path) so the user can approve it in plain text by
    recording an approval — never a silent drop, never an unattended post.

Default DRAFT_OR_ASK: the new default mode publishes draft-form notes
autonomously (drafts are colleague-invisible and revocable) but blocks
every other colleague-visible mutation. The user satisfies the gate
**without a TTY** via ``t3 review approve-on-behalf <target> <action>
--approver <id>`` (the #777/#953 interactive-TTY-only anti-pattern is
deliberately avoided).

The ORM-model imports (``OnBehalfApproval`` / ``OnBehalfAudit``) live
inside :func:`require_on_behalf_approval` rather than at module top
because ``teatree.cli.review_on_behalf.check_on_behalf`` imports this
module lazily so the ``teatree.cli`` package can be loaded before
``django.setup()`` runs (typer command discovery, ``--help`` rendering,
the privacy-scan subprocess). An eager ORM import here would defeat
the lazy chain and crash the CLI with ``ImproperlyConfigured`` (see
souliane/teatree#1003).
"""

from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict


class OnBehalfPostBlockedError(RuntimeError):
    """BLOCK verdict and no recorded approval — the on-behalf post must NOT publish.

    Carries ``target``/``action`` plus a user-facing message that names the
    exact ``t3 review approve-on-behalf`` invocation that satisfies the
    gate, so the blocked post can be surfaced to the user verbatim.
    """

    def __init__(self, target: str, action: str) -> None:
        self.target = target
        self.action = action
        super().__init__(
            f"on-behalf post blocked by on_behalf_post_mode (#960): "
            f"{action} on {target!r} needs explicit user approval first. "
            f"The user records it (no terminal required) with:\n"
            f"    t3 review approve-on-behalf {target!r} {action} --approver <user-id>\n"
            f"then the agent re-runs this post. Never publish unattended."
        )


def require_on_behalf_approval(*, target: str, action: str) -> None:
    """Gate one on-behalf post against the tri-state mode.

    See module docstring for the four-outcome table. Fail-closed: an
    unresolved (default) setting maps to
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`, and that mode
    BLOCKs every action that isn't in
    :data:`~teatree.on_behalf_gate._DRAFT_FORM_ACTIONS` when no recorded
    approval matches.
    """
    verdict = resolve_on_behalf_verdict(action)
    if verdict is OnBehalfVerdict.PROCEED:
        return
    if verdict is OnBehalfVerdict.AUTO_DRAFT:
        _notify_on_behalf_autodraft(target=target, action=action)
        return
    # BLOCK path — consume a recorded approval or raise.
    from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit  # noqa: PLC0415

    consumed = OnBehalfApproval.consume(target, action)
    if consumed is None:
        raise OnBehalfPostBlockedError(target, action)
    OnBehalfAudit.objects.create(
        approval=consumed,
        target=consumed.target,
        action=consumed.action,
        approver_id=consumed.approver_id,
    )


def _notify_on_behalf_autodraft(*, target: str, action: str) -> None:
    """Fire-and-forget DM the user when a draft-form post auto-publishes.

    Idempotency key ``on_behalf_autodraft:{target}:{action}`` guarantees
    one DM per (target, action) pair across retries within the
    ``BotPing`` ledger window — a second auto-publish of the same draft
    note is a no-op on the notification side (the GitLab API call still
    runs; only the DM is dedup'd).

    Never raises into the caller: ``notify_user`` already wraps every
    transport failure into a NOOP/FAILED ``BotPing`` row and returns
    ``False``. A misconfigured Slack backend must never block a
    legitimate autonomous draft-note publish.
    """
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415

    text = (
        f"Posted a draft note autonomously under your identity ({action} on `{target}`). "
        f"Drafts are not visible to colleagues until published.\n\n"
        f"Publish:   `t3 review publish-draft-notes <repo> <mr>`\n"
        f"Discard:   `t3 review delete-draft-note <repo> <mr> <note_id>` "
        f"(see `t3 review list-draft-notes <repo> <mr>` for the id)."
    )
    notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=f"on_behalf_autodraft:{target}:{action}",
    )
