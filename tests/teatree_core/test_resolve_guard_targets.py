"""``resolve_guard_targets`` — multi-channel review-broadcast routing (#1295 cap A).

Resolves one :class:`GuardTarget` per channel returned by
``OverlayConfig.get_review_broadcast_channels``. Per-channel Slack-Connect
tokens come from the bot backend; plain channels fall back to the legacy
sync token. Empty channel ids and empty tokens are skipped so the
returned list contains only usable targets.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured

from teatree.core.gates import review_request_guard


class _Config:
    def __init__(self, channels: list[tuple[str, str]], token: str = "") -> None:
        self._channels = channels
        self._token = token

    def get_review_broadcast_channels(self, repo: str = "") -> list[tuple[str, str]]:
        del repo
        return self._channels

    def get_slack_token(self) -> str:
        return self._token


@dataclass
class _Overlay:
    config: _Config


class _PlainMessaging:  # not SlackBotBackend
    pass


class _BotMessaging:
    """Stand-in for SlackBotBackend with per-channel xoxp resolution."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    def resolve_channel_token(self, channel_id: str) -> str:
        return self._tokens.get(channel_id, "")


def _patch_overlay(overlay: _Overlay) -> AbstractContextManager[object]:
    return patch("teatree.core.overlay_loader.get_overlay", return_value=overlay)


def _patch_messaging(backend: object) -> AbstractContextManager[object]:
    return patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend)


def _patch_slack_bot_class(bot_class: type) -> AbstractContextManager[object]:
    return patch("teatree.backends.slack.bot.SlackBotBackend", bot_class)


def test_returns_empty_when_overlay_misconfigured() -> None:
    with patch(
        "teatree.core.overlay_loader.get_overlay",
        side_effect=ImproperlyConfigured("no overlay"),
    ):
        assert review_request_guard.resolve_guard_targets() == []


def test_returns_empty_when_no_channels() -> None:
    overlay = _Overlay(_Config(channels=[]))
    with _patch_overlay(overlay):
        assert review_request_guard.resolve_guard_targets() == []


def test_skips_channel_with_empty_id() -> None:
    cfg = _Config(channels=[("rev", "C123"), ("blank", "")], token="xoxb-legacy")
    overlay = _Overlay(cfg)
    with _patch_overlay(overlay), _patch_messaging(_PlainMessaging()):
        targets = review_request_guard.resolve_guard_targets()
    assert len(targets) == 1
    assert targets[0].channel_id == "C123"
    assert targets[0].channel_name == "rev"
    assert targets[0].token == "xoxb-legacy"


def test_per_channel_token_used_for_slack_bot_backend() -> None:
    cfg = _Config(channels=[("rev", "C_REV"), ("ext", "C_EXT")])
    overlay = _Overlay(cfg)
    bot = _BotMessaging(tokens={"C_REV": "xoxp-A", "C_EXT": "xoxp-B"})
    with _patch_overlay(overlay), _patch_messaging(bot), _patch_slack_bot_class(_BotMessaging):
        targets = review_request_guard.resolve_guard_targets()
    tokens_by_id = {t.channel_id: t.token for t in targets}
    assert tokens_by_id == {"C_REV": "xoxp-A", "C_EXT": "xoxp-B"}


def test_skips_channel_when_bot_returns_no_token() -> None:
    cfg = _Config(channels=[("rev", "C_REV"), ("muted", "C_MUTED")])
    overlay = _Overlay(cfg)
    bot = _BotMessaging(tokens={"C_REV": "xoxp-A", "C_MUTED": ""})
    with _patch_overlay(overlay), _patch_messaging(bot), _patch_slack_bot_class(_BotMessaging):
        targets = review_request_guard.resolve_guard_targets()
    ids = [t.channel_id for t in targets]
    assert ids == ["C_REV"]


def test_skips_channel_when_legacy_token_blank() -> None:
    cfg = _Config(channels=[("rev", "C_REV")], token="")
    overlay = _Overlay(cfg)
    with _patch_overlay(overlay), _patch_messaging(_PlainMessaging()):
        assert review_request_guard.resolve_guard_targets() == []
