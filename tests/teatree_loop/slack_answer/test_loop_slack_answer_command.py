"""End-to-end test for the ``loop_slack_answer`` mgmt command (#1014).

Structural clone of ``test_loop_self_improve_command``: drives the full
path via ``call_command``, asserts the owner-gate / lease-contention
SKIPs, and the ``--json`` report shape. Only the Slack network is faked.
"""

import io
import json
import os
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from django.core.management import call_command
from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.core.gates.t3_master_gate import T3MasterGate
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease, PendingChatInjection
from teatree.types import RawAPIDict
from tests._loop_principal_env import pinned_loop_principal
from tests._t3_master_env import worker_owns_t3_master

runner = CliRunner()

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class RecordingBackend:
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


class TestLoopSlackAnswerCommand:
    def test_command_acks_seeded_message(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="thanks!")
        backend = RecordingBackend()
        out = io.StringIO()
        with (
            worker_owns_t3_master(),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
        ):
            call_command("loop_slack_answer", stdout=out, stderr=out)

        assert "OK" in out.getvalue()
        assert any(e[2] for e in backend.reactions)

    def test_command_json_report_shape(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="thanks!")
        backend = RecordingBackend()
        out = io.StringIO()
        with (
            worker_owns_t3_master(),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
        ):
            call_command("loop_slack_answer", json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["processed"] == 1
        assert payload["acked"] == 1
        assert "dispatched" in payload
        assert "covered" in payload

    def test_lease_contention_skips(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="thanks!")
        LoopLease.objects.acquire("loop-slack-answer", owner="other-pid")
        out = io.StringIO()
        with worker_owns_t3_master():
            call_command("loop_slack_answer", stdout=out, stderr=out)

        assert "SKIP" in out.getvalue()

    def test_foreign_t3_master_owner_skips(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="thanks!")
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="sess-other", owner_pid=os.getpid())
        out = io.StringIO()
        with pinned_loop_principal("sess-mine"):
            call_command("loop_slack_answer", stdout=out, stderr=out)

        assert "SKIP" in out.getvalue()
        assert PendingChatInjection.loop_unreplied().count() == 1

    def test_foreign_t3_master_owner_json_skip_payload(self) -> None:
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="sess-other", owner_pid=os.getpid())
        out = io.StringIO()
        with pinned_loop_principal("sess-mine"):
            call_command("loop_slack_answer", json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["skipped"] is True
        assert payload["skipped_reason"] == T3MasterGate.FOREIGN_OWNER.value
        assert payload["owner_session"] == "sess-other"
        assert "started_at" in payload

    def test_unclaimed_t3_master_json_skip_payload(self) -> None:
        out = io.StringIO()
        call_command("loop_slack_answer", json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["skipped"] is True
        # The two conditions are distinguishable — the pre-#3968 message conflated them.
        assert payload["skipped_reason"] == T3MasterGate.UNCLAIMED.value
        assert payload["owner_session"] == ""

    def test_lease_contention_json_skip_payload(self) -> None:
        LoopLease.objects.acquire("loop-slack-answer", owner="other-pid")
        out = io.StringIO()
        with worker_owns_t3_master():
            call_command("loop_slack_answer", json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["skipped"] is True
        assert "already running" in payload["skipped_reason"]


class TestSlackAnswerCliRun:
    def test_run_without_json_passes_no_kwargs(self) -> None:
        with patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["slack-answer", "run"])

        assert result.exit_code == 0
        call.assert_called_once_with("loop_slack_answer")


class TestSlackAnswerCliStatus:
    def test_status_empty_queue(self) -> None:
        result = runner.invoke(loop_app, ["slack-answer", "status"])

        assert result.exit_code == 0
        assert "queue empty" in result.stdout

    def test_status_with_loop_unreplied_messages(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="hi")
        PendingChatInjection.record(channel="C1", slack_ts="2.0", text="there")
        result = runner.invoke(loop_app, ["slack-answer", "status"])

        assert result.exit_code == 0
        assert "2 loop-unreplied" in result.stdout
