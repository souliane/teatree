"""The two ad-hoc CLI Slack egress sites honour the on-behalf gate (#960/#1750).

``t3 <overlay> notify post`` / ``notify react`` and ``t3 slack react`` were
raw routed/personal-xoxp egress at the CLI edge with no gate. They now route
through ``OnBehalfSlackEgress``: a colleague-surface call under ``ask`` with
no recorded approval refuses with a non-zero exit and never reaches the wire,
while a self-DM call stays ungated. ``t3 slack check``'s ``:eyes:`` ack on
the user's own inbound DM stays ungated (self branch of the same egress).
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from django.core.management import call_command

from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db

_DM_CHANNEL = "D_SELF"
_USER_ID = "U_OPERATOR"
_COLLEAGUE = "C_REVIEW"


@dataclass
class _RouteAwareFake:
    dm_channel_id: str = _DM_CHANNEL
    user_id: str = _USER_ID
    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    post_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _is_self_dm(self, channel: str) -> bool:
        return bool(channel) and channel in {self.dm_channel_id, self.user_id}

    def route_token(self, channel: str) -> str:
        return "xoxb-bot" if self._is_self_dm(channel) else "xoxp-user"

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_routed_calls.append((channel, text, thread_ts))
        return {"ok": True, "ts": "1700000000.0001"}

    def open_dm(self, user_id: str) -> str:
        return _DM_CHANNEL

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return {"ok": True, "ts": "1700000000.0001"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return "https://slack.example/p1"


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "{_USER_ID}"\non_behalf_post_mode = "{mode}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())


class TestNotifyCliBypassClosed:
    def test_notify_react_colleague_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, "ask")
        fake = _RouteAwareFake()
        monkeypatch.setattr(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            lambda _o=None: fake,
        )
        with pytest.raises(SystemExit) as exc:
            call_command("notify", "react", "--channel", _COLLEAGUE, "--ts", "1.1", "--emoji", "merge")
        assert exc.value.code == 2
        assert fake.react_routed_calls == []

    def test_notify_react_self_dm_ungated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, "ask")
        fake = _RouteAwareFake()
        monkeypatch.setattr(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            lambda _o=None: fake,
        )
        call_command("notify", "react", "--channel", _DM_CHANNEL, "--ts", "1.1", "--emoji", "eyes")
        assert fake.react_routed_calls == [(_DM_CHANNEL, "1.1", "eyes")]

    def test_notify_post_colleague_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, "ask")
        fake = _RouteAwareFake()
        monkeypatch.setattr(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            lambda _o=None: fake,
        )
        with pytest.raises(SystemExit) as exc:
            call_command("notify", "post", "--channel", _COLLEAGUE, "--text", "hi team")
        assert exc.value.code == 2
        assert fake.post_routed_calls == []
