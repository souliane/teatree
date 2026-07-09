"""Tests for the teatree-own MCP write tools (#3076).

Each tool is exercised end to end through ``FastMCP.call_tool`` against the
test DB, proving the handler reaches the same seam the ``t3`` CLI calls and
that the seam's gates fire identically over MCP.
"""

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.core.models import (
    ConfigSetting,
    DeferredQuestion,
    E2eMandatoryRun,
    MergeClear,
    Session,
    Task,
    Ticket,
    Worktree,
)
from teatree.mcp import build_server, review_seam, write_tools
from teatree.mcp.review_seam import register_review_post_seam
from tests.factories import MergeClearFactory, TaskFactory, TicketFactory
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY


def _payloads(result: Any) -> list[Any]:
    blocks = result[0] if isinstance(result, tuple) else result
    return [json.loads(block.text) for block in blocks if getattr(block, "text", None) is not None]


def _call(tool: str, args: dict[str, Any]) -> Any:
    return _payloads(async_to_sync(build_server().call_tool)(tool, args))[0]


class TestConfigSettingSetGateRefusal(TestCase):
    def test_plain_setting_is_written_with_registry_validation(self) -> None:
        result = _call("config_setting_set", {"key": "loop_cadence_seconds", "value": "120"})

        assert result["ok"] is True
        assert ConfigSetting.objects.get_effective("loop_cadence_seconds", scope="") == 120

    def test_gate_keys_are_refused_and_never_written(self) -> None:
        for key in (
            "banned_terms_gate_enabled",  # cold-hook gate wire
            "factory_score_enabled",  # feature flag
            "require_human_approval_to_merge",  # require_* training wheel
            "e2e_mandatory_gate_enabled",  # *_gate_enabled kill-switch
            "overlays",  # registry row
        ):
            with pytest.raises(Exception, match="refused"):
                _call("config_setting_set", {"key": key, "value": "false"})
            assert not ConfigSetting.objects.filter(key=key).exists()

    def test_refuse_reason_empty_for_plain_keys(self) -> None:
        assert write_tools.refuse_reason("loop_cadence_seconds") == ""
        assert write_tools.refuse_reason("clean_ignore") == ""


class TestCliErrorPrimitiveSurfacesStructured(TestCase):
    # The wrapped commands signal input errors with SystemExit/typer.Exit — a
    # BaseException FastMCP does NOT wrap, so without the guard the tool call
    # crashes. Each error path must instead surface as a caught error carrying the
    # command's own message. (pytest.raises(Exception) would NOT catch a bare
    # SystemExit, so these are RED on the unguarded code.)
    def test_config_setting_unknown_key_surfaces_message(self) -> None:
        with pytest.raises(Exception, match="not a known config setting"):
            _call("config_setting_set", {"key": "totally_unknown_setting_xyz", "value": "1"})

    def test_config_setting_invalid_json_surfaces_message(self) -> None:
        with pytest.raises(Exception, match="invalid JSON"):
            _call("config_setting_set", {"key": "loop_cadence_seconds", "value": "not-json{"})

    def test_question_answer_unknown_id_surfaces_message(self) -> None:
        with pytest.raises(Exception, match="not found or already resolved"):
            _call("question_answer", {"question_id": 999999, "text": "yes"})


class TestTaskBookkeeping(TestCase):
    def test_task_complete_marks_completed(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)

        result = _call("task_complete", {"task_id": task.pk})

        task.refresh_from_db()
        assert result["ok"] is True
        assert task.status == Task.Status.COMPLETED

    def test_task_fail_marks_failed(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)

        _call("task_fail", {"task_id": task.pk})

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


class TestQuestionAnswer(TestCase):
    def test_answers_a_pending_question(self) -> None:
        row = DeferredQuestion.record("Proceed with the rollout?")

        result = _call("question_answer", {"question_id": row.pk, "text": "yes"})

        row.refresh_from_db()
        assert result["ok"] is True
        assert row.answered_at is not None


class TestLifecycleTools(TestCase):
    def test_visit_phase_records_on_the_session(self) -> None:
        ticket = TicketFactory(state=Ticket.State.STARTED)

        _call("ticket_visit_phase", {"ticket": str(ticket.pk), "phase": "testing"})

        visited, _details = ticket.aggregate_phase_records()
        assert "testing" in visited

    def test_record_e2e_run_writes_the_attestation(self) -> None:
        ticket = TicketFactory(state=Ticket.State.STARTED)

        _call(
            "record_e2e_run",
            {
                "ticket": str(ticket.pk),
                "spec": "e2e/smoke.spec.ts",
                "result": "green",
                "head_sha": "a" * 40,
                "posted_url": "https://github.com/souliane/teatree/issues/1#issuecomment-1",
            },
        )

        assert E2eMandatoryRun.objects.filter(ticket=ticket, spec="e2e/smoke.spec.ts").exists()


class _GhStub:
    """Scripted `gh` replies: head at the reviewed SHA, not draft, green rollup.

    Keeps the keystone merge path hermetic — no gh binary, no network.
    """

    def __init__(self, head: str) -> None:
        self.head = head

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "baseRefName" in joined:
            return (0, "main", "")
        if "required_status_checks" in joined:
            return (0, '{"contexts": []}', "")
        if "headRefOid" in joined:
            return (0, self.head, "")
        if "isDraft" in joined:
            return (0, "false", "")
        if "statusCheckRollup" in joined:
            return (0, '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]', "")
        return (0, "", "")


class TestShipAndMergeGatePreservation(TestCase):
    def test_pr_create_blocks_without_visited_phases(self) -> None:
        # The shipping gate must fire identically over MCP: a worktree'd ticket
        # with no testing/reviewing phases visited ⇒ structured gate failure,
        # no state change.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket, overlay="test")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = _call("pr_create", {"ticket": str(ticket.pk)})

        ticket.refresh_from_db()
        assert result["allowed"] is False
        assert "missing" in result
        assert ticket.state == Ticket.State.STARTED

    def test_pr_merge_unknown_clear_is_refused(self) -> None:
        result = _call("pr_merge", {"clear_id": 999999})

        assert result["merged"] is False
        assert "not found" in result["error"]

    def test_pr_merge_substrate_clear_without_human_authorization_escalates(self) -> None:
        # §17.8: a substrate-class CLEAR is never auto-merged — the hold must
        # fire identically over MCP.
        clear = MergeClearFactory(substrate=True, ticket__state=Ticket.State.IN_REVIEW)

        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_GhStub(clear.reviewed_sha)):
            result = _call("pr_merge", {"clear_id": clear.pk})

        assert result["merged"] is False
        assert result["escalated"]
        assert clear.ticket.pk == MergeClear.objects.get(pk=clear.pk).ticket.pk

    def test_no_gate_satisfier_tools_exist(self) -> None:
        # approve-on-behalf / approve-live-post / e2e-bypass / recipe approve
        # must never be MCP tools — exposing them would let the agent
        # self-approve (maker≠checker).
        names = {tool.name for tool in asyncio.run(build_server().list_tools())}

        assert not {n for n in names if "approve" in n or "bypass" in n}


class _SeamRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post_draft_note(self, repo: str, mr: int, note: str) -> tuple[str, int]:
        self.calls.append(("draft", {"repo": repo, "mr": mr, "note": note}))
        return ("draft created", 0)

    def post_comment(self, repo: str, mr: int, note: str, *, live: bool = False) -> tuple[str, int]:
        self.calls.append(("comment", {"repo": repo, "mr": mr, "note": note, "live": live}))
        return ("posted", 0)


class TestReviewPostTools(TestCase):
    def test_draft_note_routes_through_the_registered_seam(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.write_tools.review_post_seam", return_value=recorder):
            result = _call(
                "review_post_draft_note",
                {"repo": "acme/widgets", "mr": 7, "note": "nit: rename"},
            )

        assert result == {"message": "draft created", "code": 0}
        assert recorder.calls[0][0] == "draft"

    def test_post_comment_threads_the_live_flag_to_the_gated_seam(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_comment",
                {"repo": "acme/widgets", "mr": 7, "note": "blocker: bug", "live": True},
            )

        assert recorder.calls[0][1]["live"] is True


class TestReviewSeamRegistration(TestCase):
    def test_cli_import_registers_the_review_service_seam(self) -> None:
        import teatree.cli  # noqa: F401, PLC0415 — the import side-effect under test registers the seam

        # The factory resolves the GitLab token via the external `glab` binary;
        # stub it so the registration proof does not depend on glab being installed.
        with patch("teatree.cli.review.service.ReviewService.get_gitlab_token", return_value="tok"):
            seam = review_seam.review_post_seam()
        assert callable(seam.post_draft_note)
        assert callable(seam.post_comment)

    def test_unregistered_seam_fails_loud(self) -> None:
        original = review_seam._factory
        review_seam.register_review_post_seam(review_seam._unregistered_factory)
        try:
            with pytest.raises(RuntimeError, match="not registered"):
                review_seam.review_post_seam()
        finally:
            register_review_post_seam(original)
