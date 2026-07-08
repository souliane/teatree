"""Parity: every existing outbound chokepoint routes through the send-proxy (#117).

The #117 done-criteria requires that EVERY outbound artifact — Slack DM/post/react
and forge PR/MR/issue comment — passes through :func:`teatree.core.send_proxy.route_send`
(so it is audited and, in enforce mode, allowlist-checked + redacted). This gate
pins each chokepoint module against silently regrowing a direct wire call that
skips the proxy, plus one behavioural proof that a real send writes a
``SendAudit`` row.
"""

import pathlib
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.config.enums import SendProxyMode
from teatree.core.models import ConfigSetting, SendAudit
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.on_behalf_egress import OnBehalfSlackEgress
from teatree.core.send_proxy import SendBlockedError
from teatree.types import RawAPIDict

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The chokepoints B4 enumerates — Slack (notify / on-behalf egress / reply
#: transport) and forge comments (the review send-routing helper the service
#: delegates its live posts to).
_CHOKEPOINTS = (
    "core/notify.py",
    "core/on_behalf_egress.py",
    "core/reply_transport.py",
    "cli/review/send_routing.py",
)


@pytest.mark.parametrize("relative", _CHOKEPOINTS)
def test_chokepoint_routes_through_the_proxy(relative: str) -> None:
    source = (SRC / relative).read_text()
    assert "route_send" in source, f"{relative} does not route through send_proxy.route_send"


class TestNotifyUserRoutesThroughProxy(TestCase):
    def test_notify_user_send_writes_a_send_audit_row(self) -> None:
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
        backend.get_permalink.return_value = "https://x.slack.com/archives/D-USER/p1700000000000000"

        sent = notify_user(
            "tests are green",
            kind=NotifyKind.INFO,
            idempotency_key="parity=1",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        row = SendAudit.objects.get()
        assert row.channel == SendAudit.Channel.SLACK.value
        assert row.action == "notify_user"
        # A bot→user DM is a self-destination: always allowed under every mode.
        assert row.allowlist_verdict == SendAudit.Verdict.ALLOWED.value


@dataclass
class _RouteAwareFake:
    """Minimal MessagingBackend with the #1750 self-vs-colleague classifier."""

    post_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _is_self_dm(self, channel: str) -> bool:
        return channel.startswith("D")

    def route_token(self, channel: str) -> str:
        return "xoxb-bot" if self._is_self_dm(channel) else "xoxp-user"

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_routed_calls.append((channel, text, thread_ts))
        return {"ok": True}


class TestEnforceBlockHonouredAtTheChokepoint(TestCase):
    def test_enforce_mode_colleague_post_is_refused_before_the_wire(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_mode", SendProxyMode.ENFORCE.value)
        backend = _RouteAwareFake()
        egress = OnBehalfSlackEgress(backend)

        with pytest.raises(SendBlockedError):
            egress.post(channel="C_ATTACKER", text="leak", target="o/r!1", action="post_comment")

        # Blocked BEFORE any wire call — nothing was posted.
        assert backend.post_routed_calls == []
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.DENIED.value
