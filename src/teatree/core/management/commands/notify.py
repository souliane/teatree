"""``t3 <overlay> notify`` — Slack egress from the shell (#1030, #1750).

``send`` is the bot→user DM peer of ``t3 <overlay> questions`` /
``t3 <overlay> pending_chat`` for the third
:class:`~teatree.core.notify.NotifyKind` (``INFO``). Sub-agent identities
(review/plan sub-agents, the codex rescue agent) can DM the user
directly when they finish work instead of handing the finding back to
the parent turn for a follow-up dispatch — the parent turn ending before
re-relaying would otherwise lose the news, and the user only reads Slack,
not chat. It delegates every hard invariant (idempotency, ``BotPing``
audit, ``OutboundClaim`` ledger, the on-behalf-post discipline baked
into ``notify_user``) to :func:`teatree.core.notify.notify_user`.

``post`` / ``react`` are the destination-routed peers (#1750). The
binding routing rule is a *single deterministic destination test*,
identical for posting and reacting: a private message *to the user* (the
user's own DM) goes through the per-overlay bot (``xoxb``), but a message
or a reaction to a *colleague* or a *channel* goes out under the user's
personal OAuth (``xoxp``) token. They route through
:meth:`SlackBotBackend.post_routed` / :meth:`~SlackBotBackend.react_routed`,
which both consult the one classifier :meth:`~SlackBotBackend.route_token`
— distinct from the Connect-conditional ``_channel_token`` policy, which
cannot tell the user's own DM from a colleague's. Both check the Slack
``ok`` field and exit non-zero loudly on failure; a ``missing_scope``
failure prints which scope to add to the user-OAuth app and that the user
must re-auth.

Every subcommand resolves the backend via
:func:`~teatree.core.backend_factory.messaging_from_overlay`, setting
``T3_OVERLAY_NAME`` for the duration of the call so the right
per-overlay credentials resolve. ``notify_user`` is imported from
``teatree.core`` directly, not the ``teatree.core.notify`` re-export, so the
module graph stays acyclic — ``teatree.core.management`` may not depend
on the top-level ``teatree.core.notify``.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.types import RawAPIDict

_MISSING_SCOPE_ERRORS = frozenset({"missing_scope", "no_permission"})


@contextmanager
def _overlay_env(overlay: str) -> Iterator[None]:
    """Set ``T3_OVERLAY_NAME`` for the call, restoring the prior value after."""
    if not overlay:
        yield
        return
    previous = os.environ.get("T3_OVERLAY_NAME")
    os.environ["T3_OVERLAY_NAME"] = overlay
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("T3_OVERLAY_NAME", None)
        else:
            os.environ["T3_OVERLAY_NAME"] = previous


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> notify`` group root."""

    def _resolve_backend(self, overlay: str) -> MessagingBackend:
        backend = messaging_from_overlay(overlay or None)
        if backend is None:
            self.stderr.write("no messaging backend configured for this overlay")
            raise SystemExit(1)
        return backend

    def _require_ok(self, response: RawAPIDict, *, action: str) -> None:
        if response.get("ok"):
            return
        error = str(response.get("error") or "unknown_error")
        if error in _MISSING_SCOPE_ERRORS:
            needed = str(response.get("needed") or "reactions:write")
            self.stderr.write(
                f"{action} failed: missing_scope. The user-OAuth (xoxp) token needs the {needed!r} "
                f"scope. Add it to the user-OAuth app's scopes and re-auth the token, "
                f"then retry.",
            )
        else:
            self.stderr.write(f"{action} failed: {error}")
        raise SystemExit(1)

    @command()
    def send(
        self,
        body: Annotated[
            str,
            typer.Argument(help="Slack mrkdwn body. Use ``-`` to read the body from stdin."),
        ],
        idempotency_key: Annotated[
            str,
            typer.Option("--idempotency-key", help="Required dedupe key (the helper enforces it)."),
        ],
        user_id: Annotated[
            str,
            typer.Option("--user-id", help="Slack user id to DM (defaults to the configured user)."),
        ] = "",
        kind: Annotated[
            str,
            typer.Option("--kind", help="Notification kind: info | answer | question."),
        ] = NotifyKind.INFO.value,
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay bot routing)."),
        ] = "",
    ) -> str:
        """Send a bot→user Slack DM (exit 0 on delivery, 1 otherwise)."""
        if not idempotency_key.strip():
            self.stderr.write("--idempotency-key must not be empty")
            raise SystemExit(2)
        try:
            kind_value = NotifyKind(kind)
        except ValueError as exc:
            self.stderr.write(f"unknown --kind {kind!r}; expected one of: {', '.join(k.value for k in NotifyKind)}")
            raise SystemExit(2) from exc

        text = sys.stdin.read() if body == "-" else body
        if not text.strip():
            self.stderr.write("notify body must not be empty")
            raise SystemExit(2)

        with _overlay_env(overlay):
            delivered = notify_user(
                text,
                kind=kind_value,
                idempotency_key=idempotency_key,
                user_id=user_id or None,
            )

        if not delivered:
            # Surface *why* delivery failed instead of a bare rc=1 (#1181):
            # the recorded BotPing row distinguishes a NOOP (no backend /
            # user_id configured) from a FAILED transport error and carries
            # the error detail, so the #1173 silent-rc=1 class is diagnosable
            # at the CLI edge and a wrapper can decide whether to fall back.
            from teatree.core.models import BotPing  # noqa: PLC0415

            row = BotPing.objects.filter(idempotency_key=idempotency_key).first()
            reason = (row.error_message or row.status) if row is not None else "no audit row recorded"
            self.stderr.write(f"notify_user did not deliver for key={idempotency_key}: {reason}")
            raise SystemExit(1)
        return f"sent ({idempotency_key})."

    @command()
    def post(
        self,
        channel: Annotated[
            str,
            typer.Option("--channel", help="Destination: the user's own DM (→bot) or a colleague/channel (→xoxp)."),
        ],
        text: Annotated[
            str,
            typer.Option("--text", help="Slack mrkdwn body. Use ``-`` to read the body from stdin."),
        ],
        thread_ts: Annotated[
            str,
            typer.Option("--thread-ts", help="Thread ``ts`` to reply into (omit to post a new top-level message)."),
        ] = "",
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay credentials)."),
        ] = "",
    ) -> str:
        """Post to a destination, token chosen by it: self-DM→bot, colleague/channel→xoxp (exit 0 on ``ok``)."""
        body = sys.stdin.read() if text == "-" else text
        if not body.strip():
            self.stderr.write("--text must not be empty")
            raise SystemExit(2)

        with _overlay_env(overlay):
            backend = self._resolve_backend(overlay)
            try:
                response = OnBehalfSlackEgress(backend).post(
                    channel=channel,
                    text=body,
                    target=channel,
                    action="cli_notify_post",
                    thread_ts=thread_ts,
                    destination=channel,
                    summary=body,
                )
            except OnBehalfPostBlockedError as blocked:
                self.stderr.write(str(blocked))
                raise SystemExit(2) from blocked

        self._require_ok(response, action="post")
        ts = str(response.get("ts") or "")
        return f"posted to {channel} (ts={ts})."

    @command()
    def react(
        self,
        channel: Annotated[
            str,
            typer.Option("--channel", help="Destination the message is in: self-DM (bot) or colleague/channel (xoxp)."),
        ],
        ts: Annotated[
            str,
            typer.Option("--ts", help="Timestamp ``ts`` of the message to react to."),
        ],
        emoji: Annotated[
            str,
            typer.Option("--emoji", help="Emoji name (with or without surrounding colons)."),
        ],
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay credentials)."),
        ] = "",
    ) -> str:
        """React on a destination, token chosen by it: self-DM→bot, colleague/channel→xoxp (exit 0 on ``ok``)."""
        name = emoji.strip().strip(":")
        if not name:
            self.stderr.write("--emoji must not be empty")
            raise SystemExit(2)

        with _overlay_env(overlay):
            backend = self._resolve_backend(overlay)
            try:
                response = OnBehalfSlackEgress(backend).react(
                    channel=channel,
                    ts=ts,
                    emoji=name,
                    target=channel,
                    action="cli_notify_react",
                    destination=channel,
                )
            except OnBehalfPostBlockedError as blocked:
                self.stderr.write(str(blocked))
                raise SystemExit(2) from blocked

        self._require_ok(response, action="react")
        return f"reacted :{name}: on {channel} ({ts})."
