"""Recorded-approval orchestration for the on-behalf pre-gate (#960/#961).

``teatree.on_behalf_gate`` holds the pure setting resolver
(``ask_before_post_on_behalf_enabled``) — it depends only on
``teatree.config`` and stays in that thin layer. The *satisfiable*
channel needs the :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
/ :class:`~teatree.core.models.on_behalf_approval.OnBehalfAudit` ORM models,
so its orchestration lives here in ``teatree.core`` (which legitimately
depends on both ``teatree.on_behalf_gate`` and ``teatree.core.models``),
exactly as #953 split ``teatree.utils.approval`` (pure) from
``teatree.core.db_approval_gate`` (ORM-backed).

:func:`require_on_behalf_approval` is the single chokepoint helper every
on-behalf publish path calls *before* it publishes. It is a real,
satisfiable, universal gate with three outcomes:

* gate **OFF** (the user trusts the overlay) → return, the post proceeds;
* gate **ON** + a recorded, unconsumed, exactly-scoped
    :class:`OnBehalfApproval` exists → consume it single-use, write an
    :class:`OnBehalfAudit` row, return — the post proceeds;
* gate **ON** + no recorded approval → raise :class:`OnBehalfPostBlockedError`.
    The caller must NOT publish; it surfaces the blocked post to the user
    (the user-notify path) so the user can approve it in plain text by
    recording an approval — never a silent drop, never an unattended post.

Default ON, fail-closed: an unresolved setting defaults to ON, and ON
with no approval blocks. The user satisfies the gate **without a TTY** via
``t3 review approve-on-behalf <target> <action> --approver <id>`` (the
#777/#953 interactive-TTY-only anti-pattern is deliberately avoided).

The ORM-model imports (``OnBehalfApproval`` / ``OnBehalfAudit``) live
inside :func:`require_on_behalf_approval` rather than at module top
because ``teatree.cli.review_on_behalf.check_on_behalf`` imports this
module lazily so the ``teatree.cli`` package can be loaded before
``django.setup()`` runs (typer command discovery, ``--help`` rendering,
the privacy-scan subprocess). An eager ORM import here would defeat
the lazy chain and crash the CLI with ``ImproperlyConfigured`` (see
souliane/teatree#1003).
"""

from teatree.on_behalf_gate import ask_before_post_on_behalf_enabled


class OnBehalfPostBlockedError(RuntimeError):
    """Gate ON and no recorded approval — the on-behalf post must NOT publish.

    Carries ``target``/``action`` plus a user-facing message that names the
    exact ``t3 review approve-on-behalf`` invocation that satisfies the
    gate, so the blocked post can be surfaced to the user verbatim.
    """

    def __init__(self, target: str, action: str) -> None:
        self.target = target
        self.action = action
        super().__init__(
            f"on-behalf post blocked by ask_before_post_on_behalf (#960): "
            f"{action} on {target!r} needs explicit user approval first. "
            f"The user records it (no terminal required) with:\n"
            f"    t3 review approve-on-behalf {target!r} {action} --approver <user-id>\n"
            f"then the agent re-runs this post. Never publish unattended."
        )


def require_on_behalf_approval(*, target: str, action: str) -> None:
    """Gate one on-behalf post; raise :class:`OnBehalfPostBlockedError` if not allowed.

    * gate OFF → return (post proceeds, no approval needed);
    * gate ON + recorded :class:`OnBehalfApproval` for exactly this
        ``target``+``action`` → consume it single-use, write an
        :class:`OnBehalfAudit`, return;
    * gate ON + no recorded approval → raise :class:`OnBehalfPostBlockedError`.

    Fail-closed: the default (unresolved/unset setting) is ON, and ON with
    no recorded approval blocks the post.
    """
    if not ask_before_post_on_behalf_enabled():
        return
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
