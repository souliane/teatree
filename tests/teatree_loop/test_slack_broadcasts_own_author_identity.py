"""Own-MR review skip must resolve identity from ``backend.identities`` (#1844 L3).

The own-author ``:eyes:``-and-dispatch skip in
:class:`teatree.loop.scanners.slack_broadcasts.SlackBroadcastsScanner` keys
off ``current_gitlab_username``. The wiring builder
``_slack_broadcasts_scanner_for`` previously derived that value solely from
``overlay.config.get_gitlab_username()`` — a getter many overlays leave at
the core default ``""``. An empty value disables the skip, so the loop
``:eyes:``-reacts and dispatches ``t3:reviewer`` on the user's OWN MRs
(maker == checker).

The durable fix reuses the self-identity path
:class:`teatree.loop.scanners.reviewer_prs.ReviewerPrsScanner` uses:
``backend.identities`` (the multi-alias operator set) with a
``host.current_user()`` fallback, so the own-author skip works regardless
of whether an overlay implements ``get_gitlab_username()``.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import ScannedBroadcast
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_broadcasts import MrState
from teatree.loop.tick_jobs import _slack_broadcasts_scanner_for
from teatree.types import RawAPIDict

CHANNEL = "C0DEMOCHAN1"
TS_A = "1779201478.501469"
OWN_MR = "https://gitlab.example.com/team/project/-/merge_requests/7432"
OWN_AUTHOR = "the-user"


@dataclass
class _FakeMessaging:
    user_id: str = "U0DEMOUSER1"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    history: dict[str, list[RawAPIDict]] = field(default_factory=dict)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def fetch_channel_history(self, *, channel: str, limit: int = 0) -> list[RawAPIDict]:
        _ = limit
        return list(self.history.get(channel, []))


@dataclass
class _EmptyUsernameConfig:
    """Overlay config that leaves ``get_gitlab_username`` at the core default ``""``."""

    channel_id: str

    def get_review_channel(self) -> tuple[str, str]:
        return ("the-review-team", self.channel_id)

    def get_gitlab_username(self) -> str:
        return ""

    def get_gitlab_token(self) -> str:
        return ""

    def get_github_token(self) -> str:
        return ""


@dataclass
class _FakeOverlay:
    config: _EmptyUsernameConfig


def _own_author_broadcast(messaging: _FakeMessaging) -> None:
    messaging.history[CHANNEL] = [
        {"text": f"please review {OWN_MR}", "ts": TS_A, "user": "USRG", "type": "message"},
    ]


class OwnAuthorBroadcastIdentityTests(TestCase):
    """The built scanner skips ``:eyes:`` + dispatch on the user's own-MR broadcast.

    Identity comes from ``backend.identities``, not the empty overlay getter.
    """

    def _build_and_scan(self, backend: OverlayBackends) -> list[ScanSignal]:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.scanners.slack_broadcasts import GlabGhMrStateClassifier  # noqa: PLC0415

        def _classify(_self: GlabGhMrStateClassifier, urls: list[str]) -> list[MrState]:
            return [MrState(url=url, merged=False, approved=False, author_username=OWN_AUTHOR) for url in urls]

        with patch.object(GlabGhMrStateClassifier, "__call__", _classify):
            scanner = _slack_broadcasts_scanner_for(backend)
            assert scanner is not None
            return scanner.scan()

    def test_own_mr_broadcast_emits_no_review_intent_and_no_eyes(self) -> None:
        messaging = _FakeMessaging()
        _own_author_broadcast(messaging)
        overlay = _FakeOverlay(config=_EmptyUsernameConfig(channel_id=CHANNEL))
        backend = OverlayBackends(
            name="acme",
            overlay=overlay,
            messaging=messaging,
            identities=(OWN_AUTHOR,),
        )

        signals = self._build_and_scan(backend)

        assert [s.kind for s in signals if s.kind == "slack.review_intent"] == []
        assert (CHANNEL, TS_A, "eyes") not in messaging.react_calls
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING

    def test_colleague_mr_broadcast_still_dispatches(self) -> None:
        messaging = _FakeMessaging()
        messaging.history[CHANNEL] = [
            {"text": f"please review {OWN_MR}", "ts": TS_A, "user": "USRG", "type": "message"},
        ]
        overlay = _FakeOverlay(config=_EmptyUsernameConfig(channel_id=CHANNEL))
        backend = OverlayBackends(
            name="acme",
            overlay=overlay,
            messaging=messaging,
            identities=("someone.else",),
        )

        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.scanners.slack_broadcasts import GlabGhMrStateClassifier  # noqa: PLC0415

        def _classify(_self: GlabGhMrStateClassifier, urls: list[str]) -> list[MrState]:
            return [MrState(url=url, merged=False, approved=False, author_username=OWN_AUTHOR) for url in urls]

        with patch.object(GlabGhMrStateClassifier, "__call__", _classify):
            scanner = _slack_broadcasts_scanner_for(backend)
            assert scanner is not None
            signals = scanner.scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        assert (CHANNEL, TS_A, "eyes") in messaging.react_calls


@dataclass
class _FakeHost:
    username: str

    def current_user(self) -> str:
        return self.username


class OwnAuthorIdentityResolutionTests(TestCase):
    """``_own_author_identity`` mirrors ``ReviewerPrsScanner._resolve_identities``."""

    def test_identities_take_precedence(self) -> None:
        from teatree.loop.tick_jobs import _own_author_identity  # noqa: PLC0415

        backend = OverlayBackends(
            name="acme",
            identities=(OWN_AUTHOR, "alias-2"),
            hosts=(_FakeHost("host-user"),),
        )

        assert _own_author_identity(backend) == OWN_AUTHOR

    def test_falls_back_to_first_host_current_user(self) -> None:
        from teatree.loop.tick_jobs import _own_author_identity  # noqa: PLC0415

        backend = OverlayBackends(
            name="acme",
            identities=(),
            hosts=(_FakeHost(""), _FakeHost(OWN_AUTHOR)),
        )

        assert _own_author_identity(backend) == OWN_AUTHOR

    def test_empty_when_no_identity_and_no_host_user(self) -> None:
        from teatree.loop.tick_jobs import _own_author_identity  # noqa: PLC0415

        backend = OverlayBackends(name="acme", identities=(), hosts=())

        assert _own_author_identity(backend) == ""
