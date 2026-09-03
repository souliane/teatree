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
from typing import TYPE_CHECKING

import typer

from teatree.cli.review.forge_target import owning_overlay_name
from teatree.utils.django_bootstrap import ensure_django

if TYPE_CHECKING:
    from teatree.on_behalf_gate import OnBehalfContext


def target_overlay(repo: str) -> str | None:
    """The overlay that owns *repo*, or ``None`` when it is not exactly one.

    ``t3 review <cmd> <repo> <mr> …`` names its target in the invocation, so the
    on-behalf mode governing the post is the TARGET overlay's — the same overlay
    :mod:`teatree.cli.review.forge_target` already reads the base URL and token
    from, so the gate can never judge a post by one overlay while addressing it
    with another's credential. Resolved ambiently instead, the mode is decided by
    the invoking cwd: on a multi-overlay install ``get_overlay()`` resolves
    nothing, the shipped ``DRAFT_OR_ASK`` applies, and a repo whose owning overlay
    is pinned ``immediate`` is refused anyway (souliane/teatree#960 seam,
    #3793 class).

    ``None`` for an unowned or ambiguously-owned slug. That is NOT a fall-through
    to the ambient overlay's mode: :attr:`~teatree.on_behalf_gate.OnBehalfContext.target`
    carries the named target alongside it, and a named target no single overlay owns
    drops the per-overlay tier (:attr:`~teatree.on_behalf_gate.OnBehalfContext.scope`)
    rather than inheriting an ``immediate`` pin from an overlay with no claim on it.
    The tiers ABOVE and BELOW that one still apply, so an operator's
    ``T3_ON_BEHALF_POST_MODE`` and a global workspace default both keep governing it.
    """
    return owning_overlay_name(repo) or None


def _context(repo: str, *, own_mr: bool) -> "OnBehalfContext":
    """The verdict context for a post addressed to *repo*."""
    from teatree.on_behalf_gate import OnBehalfContext  # noqa: PLC0415 — lazy CLI import

    return OnBehalfContext(overlay=target_overlay(repo) if repo else None, own_mr=own_mr, target=repo.strip())


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

    Target-less by construction: it answers "is the gate armed at all" for a
    caller that holds no repo yet. A caller that DOES hold one asks
    :func:`check_on_behalf`, which reads the target overlay's mode.

    The import stays lazy so ``teatree.cli`` is importable before
    ``django.setup()``, but it is never guarded: an unresolvable gate must surface,
    never resolve to "gate off" — a safety gate has no fail-OPEN degradation.
    """
    from teatree.on_behalf_gate import on_behalf_post_will_block  # noqa: PLC0415 — lazy CLI import

    # "approve" is a non-draft action: PROCEED under IMMEDIATE, BLOCK under ASK
    # and DRAFT_OR_ASK (AUTO_DRAFT never fires for "approve"), so the proactive
    # will-block pre-check is exactly this gate's active predicate.
    return on_behalf_post_will_block("approve")


def gate_target(repo: str, mr: int) -> str:
    """Stable ``(repo, mr)`` identifier the recorded approval scopes to.

    The on-behalf-gate target string is documented in the blocked-post
    error: it is what the user types into ``t3 review approve-on-behalf``
    when satisfying the gate.
    """
    return f"{repo}!{mr}"


def issue_gate_target(repo: str, issue_iid: int) -> str:
    """Stable ``(repo, issue)`` identifier the recorded approval scopes to.

    The issue/work-item twin of :func:`gate_target`. Uses ``#`` (not ``!``)
    so an issue-note action can never satisfy — or be satisfied by — an
    approval scoped to the same-numbered MR. It is the target the user types
    into ``t3 review approve-on-behalf`` to authorise an issue-note delete.
    """
    return f"{repo}#{issue_iid}"


def check_on_behalf(repo: str, mr: int, action: str, *, own_mr: bool = False) -> str:
    """Return an actionable error string when the on-behalf gate refuses, else ``""``.

    The *non-consuming* peek (#1879): the caller short-circuits with
    ``(message, 1)`` on a non-empty return, so no GitLab API call is
    attempted while the gate is on and no recorded :class:`OnBehalfApproval`
    matches. It never consumes — the single-use approval is consumed
    atomically with the actual GitLab post via :func:`publish_on_behalf`, so
    a peek that passes here never burns the approval if a later check refuses
    or the post fails.

    The gate covers colleague-VISIBLE actions only: a draft-form *action*
    (``post_draft_note``) is exempt under every mode and always returns
    ``""`` here — a draft is colleague-invisible and needs no approval.

    *own_mr* is the caller's PROVED owner-authorship of this MR
    (:func:`teatree.cli.review.own_mr.owner_authored_mr`). It exempts an
    author-side reply and nothing else; unset, the gate resolves exactly as
    before, so a caller that does not prove authorship stays gated.
    """
    from teatree.core.on_behalf_gate_recorded import on_behalf_block_message  # noqa: PLC0415 — lazy CLI import

    return on_behalf_block_message(gate_target(repo, mr), action, context=_context(repo, own_mr=own_mr))


def publish_on_behalf[T](repo: str, mr: int, action: str, publish: Callable[[], T], *, own_mr: bool = False) -> T:
    """Run *publish* atomically with the on-behalf consume + audit (#1879).

    The consuming half of the split: every ``ReviewService`` GitLab post
    goes through here so consume, the GitLab call, and the audit share one
    ``transaction.atomic``. A post that raises rolls back the consume (the
    approval survives a retry) and writes no audit; a BLOCK with no recorded
    approval raises :class:`OnBehalfPostBlockedError` before *publish* runs.
    *own_mr* must carry the same proved value :func:`check_on_behalf` peeked
    with, or the peek and the publish disagree about the same post.
    """
    from teatree.core.on_behalf_gate_recorded import require_on_behalf_approval  # noqa: PLC0415 — lazy CLI import

    return require_on_behalf_approval(
        target=gate_target(repo, mr), action=action, publish=publish, context=_context(repo, own_mr=own_mr)
    )


def check_on_behalf_issue(repo: str, issue_iid: int, action: str) -> str:
    """Issue/work-item twin of :func:`check_on_behalf` — non-consuming peek scoped to the issue.

    Returns an actionable error when the on-behalf gate refuses (gate ON and no
    recorded :class:`OnBehalfApproval` matches ``(<repo>#<issue>, <action>)``),
    else ``""``.
    """
    from teatree.core.on_behalf_gate_recorded import on_behalf_block_message  # noqa: PLC0415 — lazy CLI import

    return on_behalf_block_message(issue_gate_target(repo, issue_iid), action, context=_context(repo, own_mr=False))


def publish_on_behalf_issue[T](repo: str, issue_iid: int, action: str, publish: Callable[[], T]) -> T:
    """Issue/work-item twin of :func:`publish_on_behalf` — atomic consume + audit scoped to the issue.

    A BLOCK with no recorded approval raises :class:`OnBehalfPostBlockedError`
    before *publish* runs; a post that raises rolls back the consume so the
    approval survives a retry.
    """
    from teatree.core.on_behalf_gate_recorded import require_on_behalf_approval  # noqa: PLC0415 — lazy CLI import

    return require_on_behalf_approval(
        target=issue_gate_target(repo, issue_iid), action=action, publish=publish, context=_context(repo, own_mr=False)
    )


class _PublishRefusedError(RuntimeError):
    """A publish body that reported failure as ``(message, rc != 0)`` rather than raising.

    Carries the body's own return tuple so :func:`_surface` re-emits it verbatim
    after the raise has rolled the enclosing ``transaction.atomic`` back.
    """

    def __init__(self, result: tuple[str, int]) -> None:
        super().__init__(result[0])
        self.result = result


def _raising_on_failure(body: Callable[[], tuple[str, int]]) -> Callable[[], tuple[str, int]]:
    """Wrap *body* so a nonzero return code rolls the approval consume + audit back.

    The publish bodies signal failure by returning ``(message, 1)``, which commits
    the enclosing atomic block — burning the single-use approval and writing an
    audit for a post that never landed. Turning that into a raise makes a returned
    failure roll back exactly like a raised one.
    """

    def run() -> tuple[str, int]:
        result = body()
        if result[1]:
            raise _PublishRefusedError(result)
        return result

    return run


def publish_or_blocked(
    repo: str,
    mr: int,
    action: str,
    body: Callable[[], tuple[str, int]],
    *,
    own_mr: bool = False,
) -> tuple[str, int]:
    """Run *body* (the GitLab post) atomically with the on-behalf consume + audit (#1879).

    ``check_on_behalf`` already peeked non-consuming; here the approval is
    consumed in the same ``transaction.atomic`` as the post, so a failed
    post rolls back the consume (no burn) and writes no lying audit. A
    BLOCK racing in after the peek is surfaced as ``(message, 1)``.

    A failure *returned* as ``(message, rc != 0)`` rolls back identically to a
    raised one: :func:`_raising_on_failure` re-raises it inside the atomic block
    and :func:`_surface` re-emits the body's own tuple.

    A verify-after-post failure (#2081) raises
    :class:`~teatree.cli.review.audit.ReviewArtifactNotVerifiedError` from
    *inside* ``body``, so it propagates through the same ``transaction.atomic``
    and rolls back the consume + audit exactly like a post failure — then it is
    surfaced here as ``(message, 1)`` instead of the phantom "posted" claim. A
    non-404 transport error on the read-back is NOT caught here: it propagates
    so a flaky GET surfaces as ``api_unavailable``, never a false post-failure.

    *own_mr* carries the caller's PROVED owner-authorship through to the
    verdict; unset, the gate resolves exactly as before.
    """
    return _surface(lambda: publish_on_behalf(repo, mr, action, _raising_on_failure(body), own_mr=own_mr))


def publish_or_blocked_issue(
    repo: str,
    issue_iid: int,
    action: str,
    body: Callable[[], tuple[str, int]],
) -> tuple[str, int]:
    """Issue/work-item twin of :func:`publish_or_blocked` — same atomic consume + audit, issue-scoped gate.

    Routes *body* through :func:`publish_on_behalf_issue` so the recorded
    approval the gate consumes is scoped to ``(<repo>#<issue>, <action>)``,
    never an MR. Surfaces a BLOCK / verify-after-delete failure identically.
    """
    return _surface(lambda: publish_on_behalf_issue(repo, issue_iid, action, _raising_on_failure(body)))


def _surface(run: Callable[[], tuple[str, int]]) -> tuple[str, int]:
    """Run an on-behalf publish, mapping a BLOCK or verify-after-post failure to ``(message, 1)``."""
    from teatree.cli.review.audit import ReviewArtifactNotVerifiedError  # noqa: PLC0415 — lazy pre-django.setup import
    from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError  # noqa: PLC0415 — lazy pre-setup import

    try:
        return run()
    except _PublishRefusedError as refused:
        return refused.result
    except OnBehalfPostBlockedError as blocked:
        return str(blocked), 1
    except ReviewArtifactNotVerifiedError as unverified:
        return str(unverified), 1


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

        from teatree.core.models.on_behalf_approval import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
            OnBehalfApproval,
            OnBehalfApprovalError,
        )

        try:
            approval = OnBehalfApproval.record(target=target, action=action, approver_id=approver)
        except OnBehalfApprovalError as err:
            typer.echo(f"Refused: {err}")
            raise typer.Exit(code=1) from None
        typer.echo(f"OK recorded approval id={approval.pk} target={approval.target!r} action={approval.action!r}")
