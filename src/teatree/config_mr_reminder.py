"""Resolution of the ``[mr_reminder]`` config table (TODO-276).

Split out of :mod:`teatree.config` (a god-module), mirroring the
:mod:`teatree.config_speak` cohesion split: the schema — an ordered
``channels`` slug→channel map plus a ``default_channel`` fallback — is a
cohesive concern with a single dependency (:mod:`teatree.types`).

This module holds the *data* only. The repo-slug → channel routing
policy and the cross-repo message assembly live in
:mod:`teatree.core.mr_reminder` (the domain layer that may reach the
host-stripped namespace matcher), keeping config a pure data layer.
"""

from dataclasses import dataclass
from typing import Any, TypedDict


class MrReminderConfigDict(TypedDict):
    channels: dict[str, str]
    default_channel: str


@dataclass(frozen=True, slots=True)
class MrReminderConfig:
    """Repo-slug → Slack-channel routing for the cross-repo "my open MRs" reminder.

    ``channels`` is an ordered tuple of ``(slug_pattern, channel)`` pairs.
    Each ``slug_pattern`` is a host-stripped ``owner/repo`` path or an
    organisation-namespace prefix of one (``acme-engineering`` covers
    ``acme-engineering/*``) — the same leading-segment-prefix grammar the
    ``private_repos`` allowlist uses. ``default_channel`` is the fallback
    for an MR whose slug matches no pattern; empty means "drop unrouted
    MRs" so a misconfigured map never spams a wrong channel.
    """

    channels: tuple[tuple[str, str], ...] = ()
    default_channel: str = ""

    def to_dict(self) -> MrReminderConfigDict:
        return MrReminderConfigDict(channels=dict(self.channels), default_channel=self.default_channel)


def mr_reminder_from_table(table: dict[str, Any]) -> MrReminderConfig:
    """Build a :class:`MrReminderConfig` from a ``[mr_reminder]`` table.

    ``[mr_reminder.channels]`` is a slug→channel sub-table; insertion
    order is preserved so a longest-match tie-break in the router is
    deterministic. ``default_channel`` is an optional scalar. Non-string
    keys/values and a non-dict ``channels`` degrade to empty rather than
    raising, keeping the loader robust to a malformed override.
    """
    raw_channels = table.get("channels")
    channels: tuple[tuple[str, str], ...] = ()
    if isinstance(raw_channels, dict):
        channels = tuple(
            (str(slug), str(channel))
            for slug, channel in raw_channels.items()
            if isinstance(slug, str) and slug and isinstance(channel, str) and channel
        )
    default_channel = table.get("default_channel", "")
    return MrReminderConfig(
        channels=channels,
        default_channel=str(default_channel) if isinstance(default_channel, str) else "",
    )


def resolve_mr_reminder(raw: dict[str, Any]) -> MrReminderConfig:
    """Resolve the effective :class:`MrReminderConfig` from the raw toml root.

    Reads the top-level ``[mr_reminder]`` table, else returns defaults
    (no channels, no fallback → the reminder is inert until configured).
    """
    table = raw.get("mr_reminder")
    if isinstance(table, dict):
        return mr_reminder_from_table(table)
    return MrReminderConfig()
