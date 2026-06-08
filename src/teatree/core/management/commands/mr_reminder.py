"""``t3 <overlay> mr-reminder`` — cross-repo "my open MRs" Slack reminder (TODO-276).

Generalises a personal one-off reminder into a reusable command. ``preview``
assembles the per-channel reminder read-only (no Slack touch); ``send``
assembles it and posts each channel's message through the overlay's
messaging backend.

The command is a thin wrapper: assembly + repo→channel routing live in
:mod:`teatree.core.mr_reminder` (pure, deterministic). The only external
boundary is the per-channel post, routed + gated through the on-behalf
egress chokepoint (``OnBehalfSlackEgress.post``) like every colleague-
surface egress — a reminder channel is a colleague surface, so it is
subject to the on-behalf approval gate, never a raw backend call.
Channels come from the ``[mr_reminder]`` config table; identities from the
user's configured ``user_identity_aliases`` so cross-forge work surfaces
under one reminder.
"""

import os
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.config import get_effective_settings
from teatree.core.backend_factory import code_host_from_overlay, messaging_from_overlay
from teatree.core.mr_reminder import ChannelMessage, MrReminder, build_mr_reminder
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress


class ChannelPreview(TypedDict):
    channel: str
    count: int
    text: str


class ReminderResult(TypedDict):
    total: int
    channels: list[ChannelPreview]
    unrouted: list[str]


class SendResult(TypedDict):
    total: int
    posted: list[str]
    failed: list[str]
    unrouted: list[str]


def _overlay_name() -> str:
    return os.environ.get("T3_OVERLAY_NAME", "")


def _channel_preview(message: ChannelMessage, *, header: str) -> ChannelPreview:
    return ChannelPreview(
        channel=message.channel,
        count=len(message.lines),
        text=message.render(header=header),
    )


def _build(overlay: str) -> tuple[MrReminder | None, str]:
    """Return the assembled reminder, or ``(None, error)`` when prerequisites are missing."""
    host = code_host_from_overlay(overlay or None)
    if host is None:
        return None, "No code host configured (check overlay tokens)"
    settings = get_effective_settings(overlay or None)
    config = settings.mr_reminder
    if not config.channels and not config.default_channel:
        return None, "No [mr_reminder] channel map configured in ~/.teatree.toml"
    reminder = build_mr_reminder(
        host,
        config=config,
        identities=tuple(settings.user_identity_aliases),
    )
    return reminder, ""


class Command(TyperCommand):
    @command()
    def preview(
        self,
        *,
        header: Annotated[str, typer.Option(help="Message header line.")] = "Your open MRs",
    ) -> ReminderResult:
        """Assemble the per-channel reminder read-only (no Slack post)."""
        overlay = _overlay_name()
        reminder, error = _build(overlay)
        if reminder is None:
            return ReminderResult(
                total=0,
                channels=[ChannelPreview(channel="", count=0, text=error)],
                unrouted=[],
            )
        return ReminderResult(
            total=reminder.total,
            channels=[_channel_preview(m, header=header) for m in reminder.messages],
            unrouted=[line.render() for line in reminder.unrouted],
        )

    @command()
    def send(
        self,
        *,
        header: Annotated[str, typer.Option(help="Message header line.")] = "Your open MRs",
    ) -> SendResult:
        """Post the per-channel reminder to Slack (one message per routed channel)."""
        overlay = _overlay_name()
        reminder, error = _build(overlay)
        if reminder is None:
            self.stderr.write(error)
            raise SystemExit(1)

        backend = messaging_from_overlay(overlay or None)
        if backend is None:
            self.stderr.write("No messaging backend configured for this overlay")
            raise SystemExit(1)

        egress = OnBehalfSlackEgress(backend)
        posted: list[str] = []
        failed: list[str] = []
        for message in reminder.messages:
            text = message.render(header=header)
            try:
                response = egress.post(
                    channel=message.channel,
                    text=text,
                    target=message.channel,
                    action="cli_mr_reminder",
                    destination=message.channel,
                    summary=text,
                )
            except OnBehalfPostBlockedError as blocked:
                self.stderr.write(str(blocked))
                raise SystemExit(2) from blocked
            if response.get("ok"):
                posted.append(message.channel)
            else:
                failed.append(f"{message.channel}: {response.get('error') or 'unknown_error'}")

        if failed:
            self.stderr.write(f"failed to post to {len(failed)} channel(s): {'; '.join(failed)}")
            raise SystemExit(1)
        return SendResult(
            total=reminder.total,
            posted=posted,
            failed=failed,
            unrouted=[line.render() for line in reminder.unrouted],
        )
