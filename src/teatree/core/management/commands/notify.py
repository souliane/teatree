"""``t3 <overlay> notify`` — bot→user Slack DM from the shell (#1030).

The missing peer of ``t3 <overlay> questions`` / ``t3 <overlay>
pending_chat`` for the third :class:`~teatree.notify.NotifyKind`
(``INFO``). Sub-agent identities (review/plan sub-agents, the codex
rescue agent) can DM the user directly when they finish work instead of
handing the finding back to the parent turn for a follow-up dispatch —
the parent turn ending before re-relaying would otherwise lose the news,
and the user only reads Slack, not chat.

This is a thin wrapper: it sets ``T3_OVERLAY_NAME`` for the duration of
the call so :func:`~teatree.core.backend_factory.messaging_from_overlay`
resolves the right per-overlay bot, then delegates every hard invariant
(idempotency, ``BotPing`` audit, ``OutboundClaim`` ledger, the
on-behalf-post discipline baked into ``notify_user``) to
:func:`teatree.core.notify.notify_user` (imported from ``teatree.core``
directly, not the ``teatree.notify`` re-export, so the module graph
stays acyclic — ``teatree.core.management`` may not depend on the
top-level ``teatree.notify``).
"""

import os
import sys
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.notify import NotifyKind, notify_user


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> notify`` group root."""

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

        previous_overlay = os.environ.get("T3_OVERLAY_NAME")
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        try:
            delivered = notify_user(
                text,
                kind=kind_value,
                idempotency_key=idempotency_key,
                user_id=user_id or None,
            )
        finally:
            if overlay:
                if previous_overlay is None:
                    os.environ.pop("T3_OVERLAY_NAME", None)
                else:
                    os.environ["T3_OVERLAY_NAME"] = previous_overlay

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
