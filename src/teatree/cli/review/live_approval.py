r"""Authorization CLI for the ``--live`` post-comment gate (#1207, #126).

``t3 review approve-live-post <mr-url>`` is the satisfier for the
:class:`~teatree.core.gates.live_post_gate.LivePostBlockedError` gate. The
human authorization can arrive through EITHER of two durable channels —
the gate must not fail-closed against a legitimate one (#126):

* ``--slack-ts <ts>`` — verify the user's Slack DM at that timestamp.
    Refuses unless the message was authored by the configured user
    (``slack_user_id``), is fresh (within
    :data:`~teatree.core.models.live_post_approval.LIVE_POST_APPROVAL_TTL_MINUTES`
    minutes), and contains a whole-word, case-insensitive, unnegated
    approval phrase. The phrase list is the natural wording the user
    actually types ("post the findings", "approved", "ship it", ...),
    not three magic strings — a negated form such as ``"don't post
    live"`` still does NOT match (clause-scope negation, no fuzzy NLP).

* ``--from-on-behalf`` — accept a recorded
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
    for ``(<scope>, post_comment)`` as the authorization. The on-behalf
    approval IS the human authorization, recorded durably by
    ``t3 review approve-on-behalf <scope> post_comment --approver <id>``;
    requiring a *separate* fresh Slack ts on top of it was the lockout
    this closes (a user who already recorded the on-behalf token could
    not get the live post out).

With neither channel satisfied the gate stays blocked — no approval of
any kind, no token.

On success the helper mints a single-use
:class:`~teatree.core.models.live_post_approval.LivePostApproval` row
scoped to the canonical ``<repo>!<iid>`` token; the next matching
``t3 review post-comment <mr-url> ... --live`` invocation consumes it.

Kept in its own module so :mod:`teatree.cli.review` does not balloon
past the OOP/LOC ceiling — mirrors
:mod:`teatree.cli.review.on_behalf`.
"""

import re
from typing import TypedDict, cast

import typer

from teatree.utils.django_bootstrap import ensure_django


class SlackMessage(TypedDict, total=False):
    """The Slack ``conversations.history`` message shape used by the verifier.

    ``total=False`` because the upstream payload carries many keys the
    verifier never reads — typing only the two it consults
    (``user``, ``text``) keeps the surface minimal and the
    ``check_module_health`` typed-mapping rule satisfied.
    """

    user: str
    text: str


APPROVAL_PHRASES: tuple[str, ...] = (
    "post live",
    "submit it",
    "go ahead",
    # Natural approval wording the user actually types (#126): the
    # original three-phrase whitelist rejected ordinary approvals
    # and forced the user to learn one of three magic strings. Each
    # phrase is matched whole-word and is still rejected when negated
    # in the same clause (see :func:`_clause_has_phrase`).
    "post the findings",
    "post them",
    "post it",
    "approved",
    "approve it",
    "ship it",
)

# Action name an :class:`OnBehalfApproval` must carry to authorize a
# live post — the live, colleague-visible comment is a ``post_comment``
# on-behalf action, so a recorded approval for it IS the authorization.
_ON_BEHALF_POST_ACTION = "post_comment"


_NEGATION_TOKENS: tuple[str, ...] = (
    "don't",
    "do not",
    "not ",
    "never",
    "no ",
    "cannot",
    "can't",
    "won't",
    "shouldn't",
)


def _clause_has_phrase(clause: str, phrase: str) -> bool:
    r"""True iff *clause* contains *phrase* as a whole-word, unnegated match.

    ``\b`` boundaries reject substring false positives where the phrase
    is embedded in a longer word. Negation tokens (``don't`` / ``do not``
    / ``not`` / ``never`` / ...) anywhere in the same clause invalidate
    the match — the substring matcher's primary failure mode is matching
    ``"post live"`` inside ``"don't post live"``; a clause-scope negation
    check catches it without sentence-aware NLP.
    """
    lowered = clause.lower()
    if re.search(r"\b" + re.escape(phrase) + r"\b", lowered) is None:
        return False
    return not any(neg in lowered for neg in _NEGATION_TOKENS)


def _has_approval_phrase(text: str) -> bool:
    r"""True iff the message body contains a whole-word, unnegated approval phrase.

    Splits the body into sentence-like clauses on ``.``/``!``/``?``/``;``
    /``\n`` boundaries (every clause is checked independently), then
    requires that some clause contains an approval phrase under
    :func:`_clause_has_phrase`. This rejects:

    * ``"don't post live"`` (same clause negates the phrase)
    * ``"do NOT go ahead"`` (same clause negates the phrase)
    * ``"foopost livebar"`` (phrase is embedded, not whole-word)

    The longer-term fix is sentence-aware NLP (full negation scope,
    polarity flips, modal qualifiers); the clause-scope negation gate
    here is the minimal regex-only fix and is tracked as a class-C
    enforcement follow-up.
    """
    clauses = re.split(r"[.!?;\n]+", text)
    return any(_clause_has_phrase(clause, phrase) for clause in clauses for phrase in APPROVAL_PHRASES)


def _is_fresh(slack_ts: str, *, ttl_minutes: int) -> bool:
    """True iff the Slack ``ts`` is within ``ttl_minutes`` of now."""
    from django.utils import timezone  # noqa: PLC0415

    try:
        epoch = float(slack_ts)
    except ValueError:
        return False
    age = timezone.now().timestamp() - epoch
    return 0 <= age <= ttl_minutes * 60


def _fetch_user_message(*, slack_ts: str, user_id: str, channel: str) -> tuple[SlackMessage, str]:
    """Open the user's DM channel and return the message dict at ``slack_ts``.

    Returns ``(message, "")`` on success or ``({}, reason)`` when the
    backend is missing, the DM channel cannot be opened, or no message
    exists at the timestamp.
    """
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415

    backend = messaging_from_overlay()
    if backend is None:
        return {}, "no Slack backend configured — cannot verify the approval DM"
    target_channel = channel or backend.open_dm(user_id)
    if not target_channel:
        return {}, f"could not open a DM channel to user {user_id!r}"
    message = backend.fetch_message(channel=target_channel, ts=slack_ts)
    if not message:
        return {}, f"no Slack message found at ts={slack_ts!r} in channel {target_channel!r}"
    return cast("SlackMessage", message), ""


def _verify_slack_message(*, slack_ts: str, user_id: str, channel: str) -> tuple[str, str]:
    """Fetch the Slack DM and verify author + freshness + approval phrase.

    Returns ``(approval_text, "")`` on success or ``("", reason)`` when
    any check fails. The backend lookup goes through the same
    overlay-resolved messaging backend the ``notify_user`` helper uses,
    so a misconfigured Slack backend surfaces the same way here as it
    does in the bot→user DM path.
    """
    from teatree.core.models.live_post_approval import LIVE_POST_APPROVAL_TTL_MINUTES  # noqa: PLC0415

    message, error = _fetch_user_message(slack_ts=slack_ts, user_id=user_id, channel=channel)
    if error:
        return "", error

    msg_user = str(message.get("user", ""))
    if msg_user != user_id:
        return "", f"Slack message at ts={slack_ts!r} was authored by {msg_user!r}, not the user ({user_id!r})"

    if not _is_fresh(slack_ts, ttl_minutes=LIVE_POST_APPROVAL_TTL_MINUTES):
        return "", (
            f"Slack message at ts={slack_ts!r} is older than {LIVE_POST_APPROVAL_TTL_MINUTES} minutes — "
            "approval has expired, ask the user to re-DM"
        )

    text = str(message.get("text", ""))
    if not _has_approval_phrase(text):
        phrases = ", ".join(repr(p) for p in APPROVAL_PHRASES)
        return "", f"Slack message at ts={slack_ts!r} does not contain an approval phrase ({phrases})"

    return text, ""


def _verify_on_behalf_authorization(*, scope: str) -> tuple[str, str]:
    """Resolve a recorded on-behalf approval for ``(scope, post_comment)``.

    Returns ``(approval_ref, "")`` when an unconsumed
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
    exists for this exact MR scope and the ``post_comment`` action — the
    durable human authorization is sufficient to mint the live-post
    token, no Slack ts required (#126). Returns ``("", reason)`` when no
    matching approval exists; the caller then refuses.

    The approval is matched (and read) but NOT consumed here: the
    :class:`LivePostApproval` is the single-use token that the live
    post consumes. The on-behalf approval's pk is recorded on the live
    token as the audit reference for which durable authorization minted
    it.
    """
    from teatree.core.models.on_behalf_approval import OnBehalfApproval  # noqa: PLC0415

    approval = (
        OnBehalfApproval.objects.filter(
            target=scope,
            action=_ON_BEHALF_POST_ACTION,
            consumed_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )
    if approval is None:
        return "", (
            f"no recorded on-behalf approval for ({scope!r}, {_ON_BEHALF_POST_ACTION!r}) — "
            f"record one with `t3 review approve-on-behalf {scope} {_ON_BEHALF_POST_ACTION} "
            f"--approver <id>`, or pass --slack-ts <ts> with the user's DM"
        )
    return f"on-behalf-approval#{approval.pk}", ""


def _resolve_authorization(*, mr_url: str, slack_ts: str, from_on_behalf: bool, user_id: str) -> tuple[str, str, str]:
    """Resolve the authorization channel into ``(slack_ts_for_token, ref, error)``.

    Tries the on-behalf channel first when ``--from-on-behalf`` is set,
    then the Slack-ts channel. Returns ``(ts, ref, "")`` on success
    (``ts`` is the value persisted on the token — the Slack ts, or the
    on-behalf approval ref when there is no DM) or ``("", "", reason)``
    when neither channel authorizes. The caller mints the token only on
    an empty error.
    """
    from teatree.core.models.live_post_approval import canonical_mr_scope  # noqa: PLC0415

    scope = canonical_mr_scope(mr_url)
    if from_on_behalf:
        ref, error = _verify_on_behalf_authorization(scope=scope)
        if not error:
            return ref, ref, ""
        if not slack_ts:
            return "", "", error
    if not slack_ts:
        return (
            "",
            "",
            (
                "no authorization provided — pass --slack-ts <ts> (the user's approval DM) "
                "or --from-on-behalf (a recorded `t3 review approve-on-behalf` token)"
            ),
        )
    from teatree.core.notify import resolve_user_channel  # noqa: PLC0415

    _approval_text, error = _verify_slack_message(slack_ts=slack_ts, user_id=user_id, channel=resolve_user_channel())
    if error:
        return "", "", error
    return slack_ts, slack_ts, ""


def register(review_app: typer.Typer) -> None:
    """Register the ``approve-live-post`` command on the review typer app.

    Wired by :mod:`teatree.cli.review` at import-time so the command is
    part of ``t3 review`` exactly like the rest of the family, while
    the OOP/LOC ceiling stays satisfied.
    """

    @review_app.command(name="approve-live-post")
    def approve_live_post(
        mr_url: str = typer.Argument(
            help=(
                "MR reference the live-post approval is scoped to — accepts the "
                "GitLab/GitHub URL (e.g. ``https://gitlab.com/org/proj/-/merge_requests/42``) "
                "or the canonical ``<org/proj>!<iid>`` token. Single-use; consumed by "
                "the next matching ``t3 review post-comment <mr-url> ... --live``."
            )
        ),
        *,
        slack_ts: str = typer.Option(
            "",
            "--slack-ts",
            help=(
                "Slack timestamp (e.g. ``1700000000.0001``) of the user's DM authorising "
                "the live post. The helper fetches that message, refuses unless it was "
                "authored by the configured user, is recent (within the TTL window), and "
                "contains an approval phrase. Alternative to --from-on-behalf; one of the "
                "two is required."
            ),
        ),
        from_on_behalf: bool = typer.Option(
            False,
            "--from-on-behalf",
            help=(
                "Authorize from a recorded on-behalf approval instead of a Slack DM. "
                "Accepts an unconsumed `t3 review approve-on-behalf <mr-url> post_comment` "
                "token for this exact MR as the human authorization (#126). Alternative to "
                "--slack-ts; one of the two is required."
            ),
        ),
    ) -> None:
        """Mint a single-use :class:`LivePostApproval` for ``<mr-url>``.

        Authorization arrives through ``--slack-ts`` (verify the user's
        DM) OR ``--from-on-behalf`` (accept a recorded on-behalf
        approval). After this command writes the row, the next
        ``t3 review post-comment <mr-url> ... --live`` invocation
        publishes (single-use, consumed by that call); any subsequent
        live post against the same MR requires a fresh approval.
        """
        ensure_django()

        from teatree.core.models.live_post_approval import (  # noqa: PLC0415
            LivePostApproval,
            LivePostApprovalError,
            canonical_mr_scope,
        )
        from teatree.core.notify import resolve_user_id  # noqa: PLC0415

        user_id = resolve_user_id()
        if not user_id:
            typer.echo("Refused: no Slack user_id configured — set `teatree.slack_user_id` first")
            raise typer.Exit(code=1)

        token_ts, _ref, error = _resolve_authorization(
            mr_url=mr_url,
            slack_ts=slack_ts,
            from_on_behalf=from_on_behalf,
            user_id=user_id,
        )
        if error:
            typer.echo(f"Refused: {error}")
            raise typer.Exit(code=1)

        scope = canonical_mr_scope(mr_url)
        try:
            approval = LivePostApproval.record(mr_url=scope, slack_ts=token_ts, slack_user_id=user_id)
        except LivePostApprovalError as err:
            typer.echo(f"Refused: {err}")
            raise typer.Exit(code=1) from None
        typer.echo(
            f"OK recorded live-post approval id={approval.pk} mr_url={approval.mr_url!r} ts={approval.slack_ts!r}"
        )
