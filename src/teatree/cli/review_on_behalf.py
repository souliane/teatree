"""On-behalf-gate hooks and the ``approve-on-behalf`` command (#960).

Kept separate from :mod:`teatree.cli.review` so the GitLab-MR review
mechanics module stays under the OOP/LOC ceiling
(``hooks/scripts/check_module_health.py``). Two distinct concerns live
here:

* :func:`gate_target` and :func:`check_on_behalf` — the chokepoint
    helpers every ``ReviewService`` method that publishes to an MR calls
    *before* it hits the GitLab API. Returns the actionable
    ``OnBehalfPostBlockedError`` message when the gate is on and no
    recorded approval matches; returns ``""`` (proceed) otherwise.
* :func:`approve_on_behalf` — the typer command the gate's blocked-post
    message names. Records an :class:`OnBehalfApproval` so the next
    matching on-behalf attempt publishes and the row is consumed
    (no-TTY satisfier — see ``teatree.on_behalf_gate``).

Both helpers import their Django-backed dependencies lazily so the
``teatree.cli`` package can be imported (by typer for command
discovery, by a privacy-scan subprocess, etc.) before
``django.setup()`` has run.
"""

from collections.abc import Callable

import typer

from teatree.utils.django_bootstrap import ensure_django


def on_behalf_gate_active() -> bool:
    """Whether the on-behalf pre-gate forbids unattended ``approve``/``unapprove``.

    An MR approval/unapproval is an outward, state-changing post made under
    the user's identity, so it must respect the tri-state
    ``on_behalf_post_mode`` pre-gate (souliane/teatree#960). Approve is
    not a draft-form action: it is gated (returns ``True`` from this
    helper) under both :attr:`~teatree.config.OnBehalfPostMode.ASK` and
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`, and only
    permitted (returns ``False``) under
    :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE`.

    Wired through a soft import so this command works whether or not the
    gate PR has merged yet: if the module is absent the gate is treated
    as inactive (no behaviour change until it lands).
    """
    try:
        from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict  # noqa: PLC0415
    except ModuleNotFoundError:
        return False
    # "approve" is a non-draft action: PROCEED under IMMEDIATE, BLOCK under
    # ASK and DRAFT_OR_ASK. AUTO_DRAFT never fires for "approve".
    return resolve_on_behalf_verdict("approve") is not OnBehalfVerdict.PROCEED


def gate_target(repo: str, mr: int) -> str:
    """Stable ``(repo, mr)`` identifier the recorded approval scopes to.

    The on-behalf-gate target string is documented in the blocked-post
    error: it is what the user types into ``t3 review approve-on-behalf``
    when satisfying the gate.
    """
    return f"{repo}!{mr}"


def check_on_behalf(repo: str, mr: int, action: str) -> str:
    """Return an actionable error string when the on-behalf gate refuses, else ``""``.

    The *non-consuming* peek (#1879): the caller short-circuits with
    ``(message, 1)`` on a non-empty return, so no GitLab API call is
    attempted while the gate is on and no recorded :class:`OnBehalfApproval`
    matches. It never consumes — the single-use approval is consumed
    atomically with the actual GitLab post via :func:`publish_on_behalf`, so
    a peek that passes here never burns the approval if a later check refuses
    or the post fails.
    """
    from teatree.core.on_behalf_gate_recorded import on_behalf_block_message  # noqa: PLC0415

    return on_behalf_block_message(gate_target(repo, mr), action)


def publish_on_behalf[T](repo: str, mr: int, action: str, publish: Callable[[], T]) -> T:
    """Run *publish* atomically with the on-behalf consume + audit (#1879).

    The consuming half of the split: every ``ReviewService`` GitLab post
    goes through here so consume, the GitLab call, and the audit share one
    ``transaction.atomic``. A post that raises rolls back the consume (the
    approval survives a retry) and writes no audit; a BLOCK with no recorded
    approval raises :class:`OnBehalfPostBlockedError` before *publish* runs.
    """
    from teatree.core.on_behalf_gate_recorded import require_on_behalf_approval  # noqa: PLC0415

    return require_on_behalf_approval(target=gate_target(repo, mr), action=action, publish=publish)


def register(review_app: typer.Typer) -> None:
    """Register the ``approve-on-behalf`` command on the review typer app.

    Wired by :mod:`teatree.cli.review` at import-time so the command is
    part of ``t3 review`` exactly like the rest, while the OOP/LOC
    ceiling stays satisfied.
    """

    @review_app.command(name="approve-on-behalf")
    def approve_on_behalf(
        target: str = typer.Argument(
            help=(
                "Scope identifier the recorded approval is bound to — e.g. "
                "the MR ref `org/repo!42`, the PR url, or the ticket+transition "
                "compound the gate emitted in its `OnBehalfPostBlockedError` "
                "message."
            )
        ),
        action: str = typer.Argument(
            help=(
                "Action name the recorded approval authorises — exactly the "
                "string in the gate's blocked-post message (`post_comment`, "
                "`reply_to_discussion`, `approval_reaction`, etc.). Single-use; "
                "consumed when the next matching on-behalf attempt publishes."
            )
        ),
        *,
        approver: str = typer.Option(
            ...,
            "--approver",
            help=(
                "Identifier of the human user recording the approval. Refused "
                "if it names a maker/coding-agent/loop role — the executing "
                "agent can never self-authorize the post (#960, mirrors "
                "DbApproval #953 / MergeClear section 17.8)."
            ),
        ),
    ) -> None:
        """Record an :class:`OnBehalfApproval` that satisfies the on-behalf gate.

        The recorded-approval channel is the no-TTY satisfier for the
        ``on_behalf_post_mode`` pre-gate (#960, BLOCK verdict). It mirrors the
        #953 ``DbApproval`` / section 17.4 ``MergeClear`` shape:
        durable, single-use, strictly scoped to one
        ``(target, action)`` pair, maker!=checker enforced. After this
        command writes the row, the next on-behalf attempt matching
        ``(target, action)`` publishes and the row is consumed; an
        audit row records who/what/when.
        """
        ensure_django()

        from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfApprovalError  # noqa: PLC0415

        try:
            approval = OnBehalfApproval.record(target=target, action=action, approver_id=approver)
        except OnBehalfApprovalError as err:
            typer.echo(f"Refused: {err}")
            raise typer.Exit(code=1) from None
        typer.echo(f"OK recorded approval id={approval.pk} target={approval.target!r} action={approval.action!r}")
