r"""One-step ``t3 review authorize`` for the live-post gate (#126).

Getting one live, colleague-visible comment out under the user's identity
used to require two ceremony commands in the right order —
``approve-on-behalf <repo>!<mr> post_comment`` to record the durable
authorization, then ``approve-live-post <mr-url> --from-on-behalf`` to mint
the single-use live token — before ``post-comment --live`` consumed both.
One logical "yes, post this" demanded two commands.

``t3 review authorize <repo>!<mr> --approver <id>`` collapses the dance:
one command records the durable
:class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval` for
``(<scope>, post_comment)`` AND mints the matching single-use
:class:`~teatree.core.models.live_post_approval.LivePostApproval`, so the
next ``post-comment --live`` publishes with no second command.

:func:`resolve_live_authorization` is the consolidated helper the
``post_comment(..., live=True)`` path consults to decide whether a live
post is authorized. Its :class:`LiveAuthorization` proceeds when EITHER:

* the active posture permits the owner voice
    (the user has globally opted into autonomous posting — the ONE state
    in which no token at all is required); OR
* a recorded, unconsumed
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval` for
    ``(<scope>, <action>)`` exists (the durable human authorization).

It returns an actionable refusal naming the single ``authorize`` command
otherwise. The genuine guard is preserved: no authorization of any kind
→ refusal.

Kept in its own module (registered by :mod:`teatree.cli.review` via
:func:`register`, exactly like :mod:`teatree.cli.review.on_behalf` and
:mod:`teatree.cli.review.live_approval`) so the GitLab-MR review
mechanics module stays under the OOP/LOC ceiling.
"""

from dataclasses import dataclass

import typer

from teatree.utils.django_bootstrap import ensure_django

# The live, colleague-visible comment is a ``post_comment`` on-behalf
# action, so a recorded authorization for it IS the authorization the
# live-post chokepoint needs.
_POST_ACTION = "post_comment"


@dataclass(frozen=True, slots=True)
class LiveAuthorization:
    """Whether a live post may proceed, and whether it must still burn a live-post token.

    ONE resolution answers both, because they are the same verdict read twice: the
    #1207 token asserted independently of it refused posts this helper had just
    declared authorized. ``refusal`` is ``""`` to proceed,
    else the actionable message.

    ``token_required`` is ``False`` for exactly ONE state — a PROCEED verdict reached
    under a permitting posture, the owner's global opt-in to autonomous posting, which
    is the state this module's docstring calls "no token at all is required". A PROCEED
    reached any OTHER way still burns the token: the
    ``on_behalf_auto_actions`` allowlist is documented for the owner's routine
    self-documentation on their own ticket, so naming ``post_comment`` in it would
    otherwise waive a colleague-visible publish seal through a setting that never
    mentions one — a widening no governing document states, and the direction a safety
    gate must never drift on its own.
    """

    refusal: str
    token_required: bool


def resolve_live_authorization(*, scope: str, action: str = _POST_ACTION) -> LiveAuthorization:
    """Whether a live post on ``scope`` is authorized, and whether it still needs the token.

    Consults, in order:

    * the active posture — under a permitting one no token is needed (the
        user opted into autonomous posting globally), and
        :attr:`LiveAuthorization.token_required` carries that to the
        publish chokepoint so the two gates cannot disagree. The waiver is
        the POSTURE's, not the verdict's: a PROCEED the
        ``on_behalf_auto_actions`` allowlist produced authorizes the post
        and still burns the token;
    * a recorded, unconsumed
        :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
        for ``(<scope>, <action>)`` — the durable human authorization a
        single ``t3 review authorize`` records. This one authorizes the post
        but does NOT waive the token: the posture is still forbidding.

    The posture is box-global; the carve-out allowlist is read for the overlay that
    OWNS the scope's repo
    (:func:`~teatree.cli.review.on_behalf.target_overlay`), so a multi-overlay
    install judges a live post by the target's own data rather than the invoking
    cwd's. Only an MR scope carries a repo — a ticket compound or a Slack ref
    names none, and resolves ambiently exactly as before.

    The approval is matched but NOT consumed here — the chokepoints in
    the post path (``publish_on_behalf`` + ``publish_live_post``) own the
    single-use consume, each atomically with the post it guards. This helper
    is the read-only decision used to produce the user-facing refusal message.
    """
    from teatree.cli.review.on_behalf import target_overlay  # noqa: PLC0415 — lazy CLI import
    from teatree.core.models.on_behalf_approval import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        OnBehalfApproval,
        canonical_on_behalf_target,
    )
    from teatree.core.on_behalf_gate_recorded import (  # noqa: PLC0415 — lazy CLI import
        owner_voice_permitted,
        resolve_posture_verdict,
    )
    from teatree.on_behalf_gate import OnBehalfContext, OnBehalfVerdict  # noqa: PLC0415 — lazy CLI import

    clean_scope = canonical_on_behalf_target(scope)
    repo = clean_scope.split("!", 1)[0] if "!" in clean_scope else ""
    context = OnBehalfContext(overlay=target_overlay(repo) if repo else None, target=repo)
    if resolve_posture_verdict(action, context) is OnBehalfVerdict.PROCEED:
        return LiveAuthorization(refusal="", token_required=not owner_voice_permitted())

    if OnBehalfApproval.objects.filter(target=clean_scope, action=action, consumed_at__isnull=True).exists():
        return LiveAuthorization(refusal="", token_required=True)
    return LiveAuthorization(
        refusal=(
            f"live post on {clean_scope!r} is not authorized. Record one durable "
            f"authorization (no terminal required) with:\n"
            f"    t3 review authorize {clean_scope} --approver <user-id>\n"
            f"then re-run the live post. Never publish unattended."
        ),
        token_required=True,
    )


def register(review_app: typer.Typer) -> None:
    """Register the ``authorize`` command on the review typer app.

    Wired by :mod:`teatree.cli.review` at import-time so the command is
    part of ``t3 review`` exactly like the rest of the family, while the
    OOP/LOC ceiling stays satisfied.
    """

    @review_app.command(name="authorize")
    def authorize(
        scope: str = typer.Argument(
            help=(
                "MR reference the authorization is scoped to — accepts the "
                "GitLab/GitHub URL (e.g. ``https://gitlab.com/org/proj/-/merge_requests/42``) "
                "or the canonical ``<org/proj>!<iid>`` token. Records ONE durable "
                "authorization that lets the next ``t3 review post-comment <mr> ... "
                "--live`` publish — no separate ``approve-live-post`` step."
            )
        ),
        *,
        approver: str = typer.Option(
            ...,
            "--approver",
            help=(
                "Identifier of the human user recording the authorization. Refused "
                "if it names a maker/coding-agent/loop role — the executing agent "
                "can never self-authorize the post (#960, mirrors DbApproval #953 / "
                "MergeClear section 17.8)."
            ),
        ),
    ) -> None:
        """Record a one-step authorization that lets ``post-comment --live`` publish.

        Collapses the two-command dance (``approve-on-behalf`` +
        ``approve-live-post``) into one: writes the durable
        :class:`OnBehalfApproval` for ``(<scope>, post_comment)`` AND
        mints the single-use :class:`LivePostApproval` for the same MR,
        so the next matching ``t3 review post-comment <mr> ... --live``
        invocation publishes and consumes both tokens. Any subsequent
        live post on the same MR requires a fresh ``authorize``.
        """
        ensure_django()

        from teatree.core.models.live_post_approval import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
            LivePostApproval,
            canonical_mr_scope,
        )
        from teatree.core.models.on_behalf_approval import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
            OnBehalfApproval,
            OnBehalfApprovalError,
        )

        clean_scope = canonical_mr_scope(scope)
        try:
            approval = OnBehalfApproval.record(target=clean_scope, action=_POST_ACTION, approver_id=approver)
        except OnBehalfApprovalError as err:
            typer.echo(f"Refused: {err}")
            raise typer.Exit(code=1) from None

        LivePostApproval.record(
            mr_url=clean_scope,
            slack_ts=f"on-behalf-approval#{approval.pk}",
            slack_user_id=approver,
        )
        typer.echo(
            f"OK authorized live post on {clean_scope!r} (on-behalf approval id={approval.pk}). "
            f"Run `t3 review post-comment {clean_scope} <note> --live` to publish."
        )
