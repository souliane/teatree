"""A colleague post that did not LAND must not spend its approval or retire a question.

Two halves of one contract — the gate consumes a single-use ``OnBehalfApproval``
and writes an ``OnBehalfAudit`` around ``publish``, and ``deliver_user_dm``'s
threaded self-DM answer retires the queued question it replies to. Both ran on
the mere RETURN of the wire call, so a Slack ``{"ok": false, "error":
"missing_scope"}`` burned the approval / recorded an audit for a post nobody saw,
and stamped a question answered by a DM Slack refused.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import OnBehalfApproval, PendingChatInjection
from teatree.core.models.on_behalf_approval import OnBehalfAudit
from teatree.core.on_behalf_egress import OnBehalfSlackEgress
from teatree.types import RawAPIDict

_DM_CHANNEL = "D_SELF"
_USER_ID = "U_OPERATOR"
_COLLEAGUE = "C_REVIEW"
_TARGET = "https://github.com/o/r/pull/1"
#: the canonical scope ``OnBehalfApproval.record`` stores a PR-URL target under
_CANON = "o/r!1"
_APPROVER = "U-OPERATOR"
_QUESTION_TS = "1780757338.674389"


def _seed_cold_slack_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "config.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'slack_user_id', ?)",
            (json.dumps(_USER_ID),),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class _Fake:
    """Messaging backend whose wire calls and DM delivery are separately scripted."""

    routed_response: RawAPIDict = field(default_factory=lambda: {"ok": True})
    dm_response: RawAPIDict = field(default_factory=lambda: {"ok": True, "ts": "1700000000.0001"})

    def _is_self_dm(self, channel: str) -> bool:
        return bool(channel) and channel in {_DM_CHANNEL, _USER_ID}

    def route_token(self, channel: str) -> str:
        return "xoxb-bot" if self._is_self_dm(channel) else "xoxp-user"

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return dict(self.routed_response)

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return dict(self.routed_response)

    def open_dm(self, user_id: str) -> str:
        return _DM_CHANNEL

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return dict(self.dm_response)

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return "https://slack.example/p1"


class TestApprovalSurvivesAnUnlandedColleaguePost(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_cold_slack_user(tmp_path, monkeypatch)
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "ask")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _Fake())

    def test_ok_false_react_leaves_the_approval_unconsumed_and_unaudited(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="merge_reaction", approver_id=_APPROVER)
        fake = _Fake(routed_response={"ok": False, "error": "missing_scope"})

        response = OnBehalfSlackEgress(fake).react(
            channel=_COLLEAGUE,
            ts="1.1",
            emoji="merge",
            target=_TARGET,
            action="merge_reaction",
        )

        assert response == {"ok": False, "error": "missing_scope"}
        assert OnBehalfApproval.objects.get(target=_CANON, action="merge_reaction").consumed_at is None
        assert not OnBehalfAudit.objects.exists()

    def test_empty_body_post_leaves_the_approval_unconsumed(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="review_nag_post", approver_id=_APPROVER)
        fake = _Fake(routed_response={})

        response = OnBehalfSlackEgress(fake).post(
            channel=_COLLEAGUE,
            text="nag",
            target=_TARGET,
            action="review_nag_post",
        )

        assert response == {}
        assert OnBehalfApproval.objects.get(target=_CANON, action="review_nag_post").consumed_at is None
        assert not OnBehalfAudit.objects.exists()

    def test_already_reacted_still_counts_as_landed(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="merge_reaction", approver_id=_APPROVER)
        fake = _Fake(routed_response={"ok": False, "error": "already_reacted"})

        OnBehalfSlackEgress(fake).react(
            channel=_COLLEAGUE,
            ts="1.1",
            emoji="merge",
            target=_TARGET,
            action="merge_reaction",
        )

        assert OnBehalfApproval.objects.get(target=_CANON, action="merge_reaction").consumed_at is not None
        assert OnBehalfAudit.objects.count() == 1

    def test_successful_post_still_consumes_and_audits(self) -> None:
        OnBehalfApproval.record(target=_TARGET, action="review_nag_post", approver_id=_APPROVER)

        OnBehalfSlackEgress(_Fake()).post(
            channel=_COLLEAGUE,
            text="nag",
            target=_TARGET,
            action="review_nag_post",
        )

        assert OnBehalfApproval.objects.get(target=_CANON, action="review_nag_post").consumed_at is not None
        assert OnBehalfAudit.objects.count() == 1


class TestQuestionRetiresOnlyOnAnAcceptedAnswer(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_cold_slack_user(tmp_path, monkeypatch)
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "ask")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _Fake())

    def _record_question(self) -> None:
        PendingChatInjection.record(channel=_DM_CHANNEL, slack_ts=_QUESTION_TS, text="why was it cancelled?")

    def test_refused_dm_leaves_the_question_pending(self) -> None:
        self._record_question()
        fake = _Fake(dm_response={"ok": False, "error": "missing_scope"})

        OnBehalfSlackEgress(fake).post(
            channel=_DM_CHANNEL,
            text="it raced the migration",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts=_QUESTION_TS,
        )

        row = PendingChatInjection.objects.get()
        assert row.answered_at is None
        assert row.loop_replied_at is None

    def test_accepted_dm_without_a_timestamp_leaves_the_question_pending(self) -> None:
        self._record_question()
        fake = _Fake(dm_response={"ok": True})

        OnBehalfSlackEgress(fake).post(
            channel=_DM_CHANNEL,
            text="answer",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts=_QUESTION_TS,
        )

        assert PendingChatInjection.objects.get().answered_at is None

    def test_delivered_answer_still_retires_the_question(self) -> None:
        self._record_question()

        OnBehalfSlackEgress(_Fake()).post(
            channel=_DM_CHANNEL,
            text="answer",
            target=_DM_CHANNEL,
            action="cli_notify_post",
            thread_ts=_QUESTION_TS,
        )

        assert PendingChatInjection.objects.get().answered_at is not None
