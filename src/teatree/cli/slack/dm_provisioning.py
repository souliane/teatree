"""``t3 setup`` IM provisioning — open the per-overlay bot's IM once (#1342).

Each overlay's bot only routes DMs through its own IM channel; without one,
``messaging_from_overlay(<name>)`` returns a backend that hits
``channel_not_found`` on first ``chat.postMessage`` and the DM silently
falls back to whichever bot already had an IM open with the user — the
per-overlay attribution leak the issue reports.

This module is invoked from the ``t3 setup`` callback for every overlay whose
entry in the DB ``overlays`` registry declares ``messaging_backend = "slack"``
and has a token reference but no cached ``slack_dm_channel_id`` yet. It calls
``conversations.open`` once and persists the resulting channel id back into that
overlay's entry in the ``overlays`` registry row. The runtime
``messaging_from_overlay`` chain threads the cached id into ``SlackBotBackend``,
which short-circuits subsequent ``open_dm`` calls for the configured user (no
extra Slack round-trip).

The user's Slack id is resolved in priority order: first ``pass slack/user-id``
(the canonical, overlay-agnostic single source of truth a wrapper script
provisions once); then the per-overlay ``slack_user_id`` already recorded by
``t3 setup slack-bot``; finally the bot's own ``auth.test`` response (last-resort
fallback returning whichever user the token was minted for — usually the bot
user, rarely the human).

Failures are surfaced at setup time rather than mid-run at first DM attempt: a
clean ``conversations.open ok:false`` produces a single
``ProvisionResult.FAILED_OPEN_DM`` row the caller renders into a typer echo.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from teatree.backends.slack.bot import SlackBotBackend
from teatree.utils.secrets import read_pass

SLACK_DM_CHANNEL_KEY = "slack_dm_channel_id"
SLACK_USER_ID_PASS_KEY = "slack/user-id"  # noqa: S105 — pass key name, not a secret

_OVERLAYS_REGISTRY_KEY = "overlays"


class _Status(Enum):
    PROVISIONED = "provisioned"
    SKIPPED_NO_BOT = "skipped_no_bot"
    SKIPPED_NO_BOT_TOKEN = "skipped_no_bot_token"  # noqa: S105 — status name, not a secret
    SKIPPED_NO_USER_ID = "skipped_no_user_id"
    SKIPPED_ALREADY_PROVISIONED = "skipped_already_provisioned"
    FAILED_OPEN_DM = "failed_open_dm"


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """Outcome of one overlay's setup-time IM provisioning attempt.

    The status constants live on this class so callers can spell the
    enum value as ``ProvisionResult.PROVISIONED`` without importing the
    private ``_Status`` enum — keeping the ``slack_dm_provisioning`` API
    surface narrow.
    """

    PROVISIONED: ClassVar[_Status] = _Status.PROVISIONED
    SKIPPED_NO_BOT: ClassVar[_Status] = _Status.SKIPPED_NO_BOT
    SKIPPED_NO_BOT_TOKEN: ClassVar[_Status] = _Status.SKIPPED_NO_BOT_TOKEN
    SKIPPED_NO_USER_ID: ClassVar[_Status] = _Status.SKIPPED_NO_USER_ID
    SKIPPED_ALREADY_PROVISIONED: ClassVar[_Status] = _Status.SKIPPED_ALREADY_PROVISIONED
    FAILED_OPEN_DM: ClassVar[_Status] = _Status.FAILED_OPEN_DM

    status: _Status
    overlay_name: str = ""
    channel_id: str = ""
    detail: str = ""


def resolve_user_slack_id(*, bot_token: str) -> str:
    """Resolve the user's Slack id for the IM-provisioning step.

    Try ``pass show slack/user-id`` first (the overlay-agnostic canonical
    source); fall back to ``auth.test`` on the bot token (a soft fallback
    that returns whichever user the token was minted for — usually the
    bot user, rarely the human; the caller decides whether to accept it).
    Returns ``""`` when neither source resolves.
    """
    from_pass = read_pass(SLACK_USER_ID_PASS_KEY)
    if from_pass:
        return from_pass
    backend = SlackBotBackend(bot_token=bot_token)
    data = backend.auth_test()
    if not data.get("ok"):
        return ""
    user_id = data.get("user_id", "")
    return user_id if isinstance(user_id, str) else ""


def _load_overlays_registry() -> dict[str, dict]:
    """Return the DB ``overlays`` registry dict (``{name: {fields}}``), or ``{}`` when unset."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415

    stored = ConfigSetting.objects.get_effective(_OVERLAYS_REGISTRY_KEY)
    return stored if isinstance(stored, dict) else {}


def _persist_dm_channel(overlay_name: str, channel_id: str) -> None:
    """Write *channel_id* into the overlay's entry of the DB ``overlays`` registry row."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415

    registry = _load_overlays_registry()
    block = registry.get(overlay_name)
    if not isinstance(block, dict):
        return
    block[SLACK_DM_CHANNEL_KEY] = channel_id
    ConfigSetting.objects.set_value(_OVERLAYS_REGISTRY_KEY, registry)


def provision_overlay_dm_channel(*, overlay_name: str) -> ProvisionResult:
    """Open the per-overlay bot's IM with the user and persist the channel id.

    Idempotent: when the overlay's ``slack_dm_channel_id`` is already set in the
    ``overlays`` registry, returns immediately with ``SKIPPED_ALREADY_PROVISIONED``.
    The persistence target is the same registry entry ``t3 setup slack-bot`` writes;
    the runtime ``messaging_from_overlay`` chain reads it back with no round-trip.
    """
    overlay_block = _load_overlays_registry().get(overlay_name)
    if not isinstance(overlay_block, dict):
        return ProvisionResult(status=ProvisionResult.SKIPPED_NO_BOT, overlay_name=overlay_name)

    precheck = _precheck_overlay_block(overlay_block, overlay_name)
    if precheck is not None:
        return precheck

    token_ref = str(overlay_block.get("slack_token_ref", ""))
    bot_token = read_pass(f"{token_ref}-bot")
    if not bot_token:
        return ProvisionResult(
            status=ProvisionResult.SKIPPED_NO_BOT_TOKEN,
            overlay_name=overlay_name,
            detail=f"no bot token at pass `{token_ref}-bot`",
        )

    user_id = str(overlay_block.get("slack_user_id", "")) or resolve_user_slack_id(bot_token=bot_token)
    if not user_id:
        return ProvisionResult(
            status=ProvisionResult.SKIPPED_NO_USER_ID,
            overlay_name=overlay_name,
            detail="slack_user_id not set on overlay and `pass slack/user-id` empty",
        )

    backend = SlackBotBackend(bot_token=bot_token, user_id=user_id)
    channel_id = backend.open_dm(user_id)
    if not channel_id:
        return ProvisionResult(
            status=ProvisionResult.FAILED_OPEN_DM,
            overlay_name=overlay_name,
            detail="Slack `conversations.open` returned ok:false (missing scope or invalid user)",
        )

    _persist_dm_channel(overlay_name, channel_id)
    return ProvisionResult(
        status=ProvisionResult.PROVISIONED,
        overlay_name=overlay_name,
        channel_id=channel_id,
    )


def _precheck_overlay_block(overlay_block: dict, overlay_name: str) -> ProvisionResult | None:
    """Reject blocks that have no Slack bot configured or are already provisioned.

    Returns the early-exit ``ProvisionResult`` when the block isn't a candidate for
    provisioning, or ``None`` when the caller should proceed to ``conversations.open``.
    """
    if str(overlay_block.get("messaging_backend", "")) != "slack":
        return ProvisionResult(status=ProvisionResult.SKIPPED_NO_BOT, overlay_name=overlay_name)
    if not str(overlay_block.get("slack_token_ref", "")):
        return ProvisionResult(status=ProvisionResult.SKIPPED_NO_BOT, overlay_name=overlay_name)
    cached = str(overlay_block.get(SLACK_DM_CHANNEL_KEY, ""))
    if cached:
        return ProvisionResult(
            status=ProvisionResult.SKIPPED_ALREADY_PROVISIONED,
            overlay_name=overlay_name,
            channel_id=cached,
        )
    return None


def provision_all_overlay_dm_channels(*, echo: Callable[[str], None]) -> list[ProvisionResult]:
    """Provision every Slack-bot overlay's IM channel; called by ``t3 setup``.

    Iterates the DB ``overlays`` registry. For every entry declaring
    ``messaging_backend = "slack"``, calls :func:`provision_overlay_dm_channel`.
    Renders one ``echo`` line per actionable result so the user sees IM
    provisioning land alongside the rest of the setup output.
    """
    results: list[ProvisionResult] = []
    for name, block in _load_overlays_registry().items():
        if not isinstance(block, dict):
            continue
        if str(block.get("messaging_backend", "")) != "slack":
            continue
        result = provision_overlay_dm_channel(overlay_name=name)
        _render(result, echo)
        results.append(result)
    return results


def _render(result: ProvisionResult, echo: Callable[[str], None]) -> None:
    """Emit a single human-readable line per provisioning outcome."""
    name = result.overlay_name
    status = result.status
    if status is ProvisionResult.PROVISIONED:
        echo(f"OK    Provisioned Slack IM for overlay `{name}` (channel {result.channel_id}).")
    elif status is ProvisionResult.SKIPPED_ALREADY_PROVISIONED:
        echo(f"OK    Slack IM for overlay `{name}` already provisioned (channel {result.channel_id}).")
    elif status is ProvisionResult.SKIPPED_NO_BOT_TOKEN or status is ProvisionResult.SKIPPED_NO_USER_ID:
        echo(f"WARN  Skipped IM provisioning for overlay `{name}`: {result.detail}.")
    elif status is ProvisionResult.FAILED_OPEN_DM:
        echo(f"ERROR IM provisioning failed for overlay `{name}`: {result.detail}.")
    # SKIPPED_NO_BOT is intentionally silent — every non-Slack overlay
    # hits this path and rendering it would produce N "skipped" lines
    # the user has no action on.


__all__ = [
    "SLACK_DM_CHANNEL_KEY",
    "SLACK_USER_ID_PASS_KEY",
    "ProvisionResult",
    "provision_all_overlay_dm_channels",
    "provision_overlay_dm_channel",
    "resolve_user_slack_id",
]
