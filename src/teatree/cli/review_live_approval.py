"""Slack-DM-verified approval CLI for the ``--live`` post-comment gate (#1207).

``t3 review approve-live-post <mr-url> --slack-ts <ts>`` is the single
satisfier for the :class:`~teatree.core.live_post_gate.LivePostBlockedError`
gate. It walks the Slack DM at ``<ts>``, refuses unless:

* the message was authored by the configured user (``slack_user_id``),
* the message is fresh — within
    :data:`~teatree.core.models.live_post_approval.LIVE_POST_APPROVAL_TTL_MINUTES`
    minutes of now,
* the message body contains an exact, case-insensitive approval phrase
    (``"post live"`` / ``"submit it"`` / ``"go ahead"``) — single-pattern
    substring match, no fuzzy NLP.

On success the helper mints a single-use
:class:`~teatree.core.models.live_post_approval.LivePostApproval` row
scoped to the canonical ``<repo>!<iid>`` token; the next matching
``t3 review post-comment <mr-url> ... --live`` invocation consumes it.

Kept in its own module so :mod:`teatree.cli.review` does not balloon
past the OOP/LOC ceiling — mirrors
:mod:`teatree.cli.review_on_behalf`.
"""

from typing import TypedDict, cast

import typer


class SlackMessage(TypedDict, total=False):
    """The Slack ``conversations.history`` message shape used by the verifier.

    ``total=False`` because the upstream payload carries many keys the
    verifier never reads — typing only the two it consults
    (``user``, ``text``) keeps the surface minimal and the
    ``check_module_health`` typed-mapping rule satisfied.
    """

    user: str
    text: str


APPROVAL_PHRASES: tuple[str, ...] = ("post live", "submit it", "go ahead")


def _user_channel() -> str:
    """Resolve the Slack DM channel id the user reads (the ``D...`` token).

    Returns ``""`` when no channel is configured (the test harness path);
    the caller treats an empty channel as "verify against the user_id
    only, do not pin to a specific DM channel".
    """
    import os  # noqa: PLC0415

    from teatree.config import load_config  # noqa: PLC0415

    cfg = load_config().raw
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
    overlays = cfg.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        channel = overlays[overlay_name].get("slack_user_channel", "")
        if channel:
            return str(channel)
    teatree_cfg = cfg.get("teatree") or {}
    return str(teatree_cfg.get("slack_user_channel", ""))


def _has_approval_phrase(text: str) -> bool:
    """True iff the message body contains an exact approval phrase (case-insensitive)."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in APPROVAL_PHRASES)


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
            ...,
            "--slack-ts",
            help=(
                "Slack timestamp (e.g. ``1700000000.0001``) of the user's DM authorising "
                "the live post. The helper fetches that message, refuses unless it was "
                "authored by the configured user, is recent (within the TTL window), and "
                "contains an explicit approval phrase (``post live`` / ``submit it`` / ``go ahead``)."
            ),
        ),
    ) -> None:
        """Mint a Slack-recorded :class:`LivePostApproval` for ``<mr-url>``.

        After this command writes the row, the next
        ``t3 review post-comment <mr-url> ... --live`` invocation
        publishes (single-use, consumed by that call); any subsequent
        live post against the same MR requires a fresh approval.
        """
        import os  # noqa: PLC0415

        import django  # noqa: PLC0415

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()

        from teatree.core.models.live_post_approval import (  # noqa: PLC0415
            LivePostApproval,
            LivePostApprovalError,
            canonical_mr_scope,
        )
        from teatree.core.notify import _resolve_user_id  # noqa: PLC0415

        user_id = _resolve_user_id()
        if not user_id:
            typer.echo("Refused: no Slack user_id configured — set `teatree.slack_user_id` first")
            raise typer.Exit(code=1)

        _approval_text, error = _verify_slack_message(
            slack_ts=slack_ts,
            user_id=user_id,
            channel=_user_channel(),
        )
        if error:
            typer.echo(f"Refused: {error}")
            raise typer.Exit(code=1)

        scope = canonical_mr_scope(mr_url)
        try:
            approval = LivePostApproval.record(mr_url=scope, slack_ts=slack_ts, slack_user_id=user_id)
        except LivePostApprovalError as err:
            typer.echo(f"Refused: {err}")
            raise typer.Exit(code=1) from None
        typer.echo(
            f"OK recorded live-post approval id={approval.pk} mr_url={approval.mr_url!r} ts={approval.slack_ts!r}"
        )
