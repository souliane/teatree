"""Route classifier fail-closed on unknown surface (TODO-11).

When the backend does not recognize a surface (no route_token classifier),
the egress must fail-closed and raise OnBehalfPostBlockedError, never
silently default to colleague or any other fallback.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import OnBehalfApproval
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.types import RawAPIDict

_DM_CHANNEL = "D_SELF"
_USER_ID = "U_OPERATOR"
_UNKNOWN_SURFACE = "X_UNKNOWN"
_TARGET = "https://github.com/o/r/pull/1"
_APPROVER = "U-OPERATOR"


@dataclass
class _UnknownSurfaceFake:
    """Fake MessagingBackend that does not implement route_token.

    Represents an unknown/unregistered surface that the classifier
    cannot recognize. The egress should fail-closed.
    """

    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    post_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_routed_calls.append((channel, text, thread_ts))
        return {"ok": True}


def _write_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "{_USER_ID}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", mode)


class TestRouteClassifierFailsClosedOnUnknownSurface(TestCase):
    """Unknown surfaces must be denied, not defaulted to colleague."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "immediate")

    def test_react_on_unknown_surface_raises_fail_closed(self) -> None:
        """React to unknown surface must fail-closed, never silently post."""
        fake = _UnknownSurfaceFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.react(
                channel=_UNKNOWN_SURFACE,
                ts="1.1",
                emoji="eyes",
                target=_TARGET,
                action="adhoc_slack_react",
            )
        assert fake.react_routed_calls == [], "Unknown surface should not reach wire"

    def test_post_on_unknown_surface_raises_fail_closed(self) -> None:
        """Post to unknown surface must fail-closed, never silently post."""
        fake = _UnknownSurfaceFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.post(
                channel=_UNKNOWN_SURFACE,
                text="test message",
                target=_TARGET,
                action="test_post",
            )
        assert fake.post_routed_calls == [], "Unknown surface should not reach wire"

    def test_unknown_surface_blocked_even_with_immediate_mode(self) -> None:
        """Unknown surface blocks even in immediate mode (bypass-all mode).

        The immediate mode is dangerously open (no pre-gate approval),
        but fail-closed on unknown surfaces transcends the post mode.
        """
        fake = _UnknownSurfaceFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.post(
                channel=_UNKNOWN_SURFACE,
                text="test message",
                target=_TARGET,
                action="test_post",
            )
        assert fake.post_routed_calls == []

    def test_unknown_surface_blocked_even_with_approval_recorded(self) -> None:
        """Unknown surface blocks even when approval is recorded.

        We cannot approve a post to an unclassifiable surface.
        """
        OnBehalfApproval.record(target=_TARGET, action="test_post", approver_id=_APPROVER)
        fake = _UnknownSurfaceFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.post(
                channel=_UNKNOWN_SURFACE,
                text="test message",
                target=_TARGET,
                action="test_post",
            )
        assert fake.post_routed_calls == []


class TestRouteClassifierUnknownSurfaceWithAskMode(TestCase):
    """Unknown surfaces fail-closed in ask mode too."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "ask")

    def test_unknown_surface_blocked_in_ask_mode(self) -> None:
        """Unknown surface is blocked before gate check, in ask mode."""
        fake = _UnknownSurfaceFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.react(
                channel=_UNKNOWN_SURFACE,
                ts="1.1",
                emoji="eyes",
                target=_TARGET,
                action="adhoc_slack_react",
            )
        assert fake.react_routed_calls == []
