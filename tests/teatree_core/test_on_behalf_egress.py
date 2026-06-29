"""OnBehalfSlackEgress — the single colleague-Slack post/react chokepoint (#960/#1750).

Symmetric coverage of the gate→route→emit→audit contract:

*   colleague surface under ``ask`` + no approval BLOCKS (raises, never
    reaches the wire);
*   the same with a recorded ``OnBehalfApproval`` FIRES exactly once and
    writes one ``on_behalf_post:`` BotPing (the audit);
*   the self-DM carve-out (the #1750 ``route_token`` classifies the user's
    own DM as self) emits ungated/unaudited and consumes no approval;
*   a backend with no ``route_token`` accessor FAILS CLOSED to the gate;
*   the after-receipt DM fires only on a *real* success — never on
    ``already_reacted`` / ``ok:false``.
"""

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import BotPing, OnBehalfApproval, PendingChatInjection
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.types import RawAPIDict

_DM_CHANNEL = "D_SELF"
_USER_ID = "U_OPERATOR"
_COLLEAGUE = "C_REVIEW"
_TARGET = "https://github.com/o/r/pull/1"
_APPROVER = "U-OPERATOR"
_QUESTION_TS = "1780757338.674389"


@dataclass
class _RouteAwareFake:
    """Fake MessagingBackend with the #1750 ``route_token`` / ``_is_self_dm`` classifier."""

    dm_channel_id: str = _DM_CHANNEL
    user_id: str = _USER_ID
    routed_response: RawAPIDict = field(default_factory=lambda: {"ok": True})
    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    post_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _is_self_dm(self, channel: str) -> bool:
        return bool(channel) and channel in {self.dm_channel_id, self.user_id}

    def route_token(self, channel: str) -> str:
        return "xoxb-bot" if self._is_self_dm(channel) else "xoxp-user"

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return dict(self.routed_response)

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_routed_calls.append((channel, text, thread_ts))
        return dict(self.routed_response)

    def open_dm(self, user_id: str) -> str:
        return _DM_CHANNEL

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return {"ok": True, "ts": "1700000000.0001"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return "https://slack.example/p1"


@dataclass
class _NoRouteFake:
    """Fake with NO ``route_token`` accessor — every surface is unclassifiable."""

    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    post_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_routed_calls.append((channel, text, thread_ts))
        return {"ok": True}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return {"ok": True, "ts": "1700000000.0001"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return "https://slack.example/p1"


def _write_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    # ``slack_user_id`` is a RAW key (TOML-home); ``on_behalf_post_mode`` is
    # DB-home (#1775) so a TOML value for it is ignored on read — stage it via
    # the ``T3_*`` env tier, which wins for a DB-home key and needs no DB.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "{_USER_ID}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", mode)


class TestColleagueGate(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "ask")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())
        self.monkeypatch = monkeypatch

    def test_react_blocks_on_colleague_without_approval(self) -> None:
        fake = _RouteAwareFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.react(channel=_COLLEAGUE, ts="1.1", emoji="merge", target=_TARGET, action="merge_reaction")
        assert fake.react_routed_calls == []
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_post_blocks_on_colleague_without_approval(self) -> None:
        fake = _RouteAwareFake()
        egress = OnBehalfSlackEgress(fake)
        with pytest.raises(OnBehalfPostBlockedError):
            egress.post(channel=_COLLEAGUE, text="nag", target=_TARGET, action="review_nag_post")
        assert fake.post_routed_calls == []

    def test_react_fires_once_and_audits_with_recorded_approval(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="merge_reaction", approver_id=_APPROVER)
        fake = _RouteAwareFake()
        response = OnBehalfSlackEgress(fake).react(
            channel=_COLLEAGUE,
            ts="1.1",
            emoji="merge",
            target=_TARGET,
            action="merge_reaction",
        )
        assert response == {"ok": True}
        assert fake.react_routed_calls == [(_COLLEAGUE, "1.1", "merge")]
        ping = BotPing.objects.get(idempotency_key=f"on_behalf_post:{_TARGET}:merge_reaction")
        assert ping.status == BotPing.Status.SENT

    def test_post_fires_once_and_audits_with_recorded_approval(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="review_nag_post", approver_id=_APPROVER)
        fake = _RouteAwareFake()
        OnBehalfSlackEgress(fake).post(
            channel=_COLLEAGUE,
            text="day-1 nag",
            target=_TARGET,
            action="review_nag_post",
        )
        assert fake.post_routed_calls == [(_COLLEAGUE, "day-1 nag", "")]
        assert BotPing.objects.filter(
            idempotency_key=f"on_behalf_post:{_TARGET}:review_nag_post",
        ).exists()

    def test_approval_consumed_single_use(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="merge_reaction", approver_id=_APPROVER)
        fake = _RouteAwareFake()
        egress = OnBehalfSlackEgress(fake)
        egress.react(channel=_COLLEAGUE, ts="1.1", emoji="merge", target=_TARGET, action="merge_reaction")
        with pytest.raises(OnBehalfPostBlockedError):
            egress.react(channel=_COLLEAGUE, ts="1.1", emoji="merge", target=_TARGET, action="merge_reaction")


class TestSelfDmCarveOut(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "ask")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())

    def test_self_dm_react_emits_ungated_unaudited(self) -> None:
        fake = _RouteAwareFake()
        response = OnBehalfSlackEgress(fake).react(
            channel=_DM_CHANNEL,
            ts="1.1",
            emoji="eyes",
            target=_DM_CHANNEL,
            action="adhoc_slack_react",
        )
        assert response == {"ok": True}
        assert fake.react_routed_calls == [(_DM_CHANNEL, "1.1", "eyes")]
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_self_dm_does_not_consume_approval(self) -> None:
        OnBehalfApproval.record(target=_DM_CHANNEL, action="adhoc_slack_react", approver_id=_APPROVER)
        OnBehalfSlackEgress(_RouteAwareFake()).react(
            channel=_USER_ID,
            ts="1.1",
            emoji="eyes",
            target=_DM_CHANNEL,
            action="adhoc_slack_react",
        )
        approval = OnBehalfApproval.objects.get(target=_DM_CHANNEL, action="adhoc_slack_react")
        assert approval.consumed_at is None

    def test_self_dm_post_emits_ungated_via_shared_dm_chokepoint(self) -> None:
        # The self-DM post routes through teatree.core.speak.deliver_user_dm
        # (the shared bot→user DM chokepoint), which posts via post_message —
        # ungated and unaudited, consuming no approval and writing no
        # on_behalf_post BotPing. With the speak feature off (default), no
        # audio attach and no local read fire.
        fake = _RouteAwareFake()
        response = OnBehalfSlackEgress(fake).post(
            channel=_DM_CHANNEL,
            text="hi",
            target=_DM_CHANNEL,
            action="cli_notify_post",
        )
        assert response.get("ok") is True
        assert fake.post_routed_calls == []
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()


class TestFailClosed(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "ask")

    def test_no_route_token_backend_blocks_under_ask(self) -> None:
        fake = _NoRouteFake()
        with pytest.raises(OnBehalfPostBlockedError):
            OnBehalfSlackEgress(fake).react(
                channel=_DM_CHANNEL,
                ts="1.1",
                emoji="eyes",
                target=_DM_CHANNEL,
                action="adhoc_slack_react",
            )
        assert fake.react_routed_calls == []


class TestAuditOnlyOnRealSuccess(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "immediate")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())

    def test_no_audit_when_ok_false(self) -> None:
        fake = _RouteAwareFake(routed_response={"ok": False, "error": "missing_scope"})
        OnBehalfSlackEgress(fake).react(
            channel=_COLLEAGUE,
            ts="1.1",
            emoji="merge",
            target=_TARGET,
            action="merge_reaction",
        )
        assert fake.react_routed_calls == [(_COLLEAGUE, "1.1", "merge")]
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_no_audit_when_already_reacted(self) -> None:
        fake = _RouteAwareFake(routed_response={"ok": False, "error": "already_reacted"})
        OnBehalfSlackEgress(fake).react(
            channel=_COLLEAGUE,
            ts="1.1",
            emoji="merge",
            target=_TARGET,
            action="merge_reaction",
        )
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_audit_fires_under_immediate_on_success(self) -> None:
        fake = _RouteAwareFake()
        OnBehalfSlackEgress(fake).react(
            channel=_COLLEAGUE,
            ts="1.1",
            emoji="merge",
            target=_TARGET,
            action="merge_reaction",
        )
        assert BotPing.objects.filter(
            idempotency_key=f"on_behalf_post:{_TARGET}:merge_reaction",
        ).exists()


class TestThreadedAnswerRetiresQuestion(TestCase):
    """The deliberate threaded self-DM answer retires its question (#2053).

    The ``notify post --thread-ts`` answer route is the only egress that
    deliberately threads a self-DM under a queued question, so the retire
    fires iff the DM is genuinely an answer — never for an unrelated INFO
    DM (see ``test_speak_chokepoint_does_not_retire``). Both gates are
    stamped: ``loop_replied_at`` (the cycle stops re-delegating an answerer
    Task) and ``answered_at`` (the Stop-hook gate stops nagging).
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "ask")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())

    def _record_question(self) -> None:
        PendingChatInjection.record(channel=_DM_CHANNEL, slack_ts=_QUESTION_TS, text="why was it cancelled?")

    def test_threaded_self_dm_answer_stamps_both_gates(self) -> None:
        self._record_question()

        OnBehalfSlackEgress(_RouteAwareFake()).post(
            channel=_DM_CHANNEL,
            text="it raced the migration",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts=_QUESTION_TS,
        )

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is not None
        assert row.answered_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_retired_question_drops_out_of_loop_unreplied(self) -> None:
        self._record_question()

        OnBehalfSlackEgress(_RouteAwareFake()).post(
            channel=_DM_CHANNEL,
            text="answer",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts=_QUESTION_TS,
        )

        assert list(PendingChatInjection.loop_unreplied()) == []

    def test_top_level_self_dm_does_not_retire(self) -> None:
        self._record_question()

        OnBehalfSlackEgress(_RouteAwareFake()).post(
            channel=_DM_CHANNEL,
            text="unrelated status",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts="",
        )

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is None
        assert row.answered_at is None

    def test_threaded_self_dm_with_unrelated_thread_leaves_question_open(self) -> None:
        self._record_question()

        OnBehalfSlackEgress(_RouteAwareFake()).post(
            channel=_DM_CHANNEL,
            text="answer to a different thread",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts="2222222222.000000",
        )

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is None
        assert row.answered_at is None

    def test_retire_failure_is_swallowed_and_dm_still_delivered(self) -> None:
        self._record_question()
        with patch.object(
            PendingChatInjection,
            "retire_answered_in_thread",
            side_effect=RuntimeError("db locked"),
        ):
            response = OnBehalfSlackEgress(_RouteAwareFake()).post(
                channel=_DM_CHANNEL,
                text="answer",
                target=_DM_CHANNEL,
                action="cli_notify_post",
                thread_ts=_QUESTION_TS,
            )

        assert response.get("ok") is True
        assert PendingChatInjection.objects.get().loop_replied_at is None


class TestUnknownSurfaceRouting(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode(tmp_path, monkeypatch, "ask")

    def test_unknown_surface_logs_explicitly_and_fails_closed(self) -> None:
        """Unknown surface (no route_token) logs surface name and fails closed to gate."""
        fake = _NoRouteFake()
        egress = OnBehalfSlackEgress(fake)

        with patch("teatree.core.on_behalf_egress.logger") as mock_logger, pytest.raises(OnBehalfPostBlockedError):
            egress.post(
                channel="C_UNKNOWN",
                text="post to unknown surface",
                target="https://github.com/o/r/pull/1",
                action="test_action",
            )

        mock_logger.warning.assert_called()
        call_args = str(mock_logger.warning.call_args)
        assert "C_UNKNOWN" in call_args or "unclassifiable" in call_args.lower()

    def test_unknown_surface_react_logs_explicitly_and_fails_closed(self) -> None:
        """Unknown surface (no route_token) for react logs surface name and fails closed to gate."""
        fake = _NoRouteFake()
        egress = OnBehalfSlackEgress(fake)

        with patch("teatree.core.on_behalf_egress.logger") as mock_logger, pytest.raises(OnBehalfPostBlockedError):
            egress.react(
                channel="C_UNKNOWN",
                ts="1.1",
                emoji="eyes",
                target="https://github.com/o/r/pull/1",
                action="test_action",
            )

        mock_logger.warning.assert_called()
        call_args = str(mock_logger.warning.call_args)
        assert "C_UNKNOWN" in call_args or "unclassifiable" in call_args.lower()
