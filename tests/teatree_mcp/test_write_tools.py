"""Tests for the teatree-own MCP write tools (#3076).

Each tool is exercised end to end through ``MCPServer.call_tool`` against the
test DB, proving the handler reaches the same seam the ``t3`` CLI calls and
that the seam's gates fire identically over MCP.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.cli.review.evidence_gate import FindingEvidence
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
from teatree.mcp import build_server, review_seam, review_write_tools, write_tools
from teatree.mcp.review_seam import SeamNote, register_review_post_seam
from tests.factories import MergeClearFactory, TaskFactory, TicketFactory
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY
from tests.teatree_mcp._call_tool_result import payloads as _payloads

_NOW = datetime.now(tz=UTC)


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
            "banned_terms",  # leak-scrub input list (cold-read)
            "overlay_leak_terms",  # leak-scrub input list (cold-read)
            "danger_gate_fail_open",  # master fail-open switch (cold-read)
        ):
            with pytest.raises(Exception, match="refused"):
                _call("config_setting_set", {"key": key, "value": "false"})
            assert not ConfigSetting.objects.filter(key=key).exists()

    def test_refuse_reason_empty_for_plain_keys(self) -> None:
        assert write_tools.refuse_reason("loop_cadence_seconds") == ""
        assert write_tools.refuse_reason("clean_ignore") == ""


class TestCliErrorPrimitiveSurfacesStructured(TestCase):
    # The wrapped commands signal input errors with SystemExit/typer.Exit — a
    # BaseException MCPServer does NOT wrap, so without the guard the tool call
    # crashes. Each error path must instead surface as a caught error carrying the
    # command's own message. (pytest.raises(Exception) would NOT catch a bare
    # SystemExit, so these are RED on the unguarded code.)
    def test_config_setting_unknown_key_surfaces_message(self) -> None:
        with pytest.raises(Exception, match="not a known config setting"):
            _call("config_setting_set", {"key": "totally_unknown_setting_xyz", "value": "1"})

    def test_config_setting_invalid_json_surfaces_message(self) -> None:
        with pytest.raises(Exception, match="invalid JSON"):
            _call("config_setting_set", {"key": "loop_cadence_seconds", "value": "not-json{"})

    def test_config_setting_inconsistent_harness_provider_pair_is_refused(self) -> None:
        # #3688: the same write-time cross-key guard fires through the MCP seam
        # (which wraps the CLI), surfacing the refusal as a structured error with
        # the store left untouched — not a silent accept that dooms every dispatch.
        with pytest.raises(Exception, match="inconsistent config"):
            _call("config_setting_set", {"key": "agent_harness_provider", "value": '"openai_compatible"'})
        assert not ConfigSetting.objects.filter(key="agent_harness_provider").exists()

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

    def post_draft_note(self, repo: str, mr: int, note: SeamNote) -> tuple[str, int]:
        self.calls.append(("draft", {"repo": repo, "mr": mr, "note": note}))
        return ("draft created", 0)

    def post_comment(self, repo: str, mr: int, note: SeamNote, *, live: bool = False) -> tuple[str, int]:
        self.calls.append(("comment", {"repo": repo, "mr": mr, "note": note, "live": live}))
        return ("posted", 0)


class TestReviewPostTools(TestCase):
    def test_draft_note_routes_through_the_registered_seam(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            result = _call(
                "review_post_draft_note",
                {"repo": "acme/widgets", "mr": 7, "finding": {"note": "nit: rename"}},
            )

        assert result == {"message": "draft created", "code": 0}
        assert recorder.calls[0][0] == "draft"

    def test_post_comment_threads_the_live_flag_to_the_gated_seam(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_comment",
                {"repo": "acme/widgets", "mr": 7, "finding": {"note": "blocker: bug"}, "live": True},
            )

        assert recorder.calls[0][1]["live"] is True

    def test_the_seam_is_built_for_the_repo_the_tool_names(self) -> None:
        """The target repo reaches the seam FACTORY, not just the post call (#3793).

        The service resolves its base URL and token from the repo it was built
        for, so a factory that never sees the repo falls back to ambient overlay
        resolution — the multi-overlay outage.
        """
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder) as seam:
            _call("review_post_draft_note", {"repo": "acme/widgets", "mr": 7, "finding": {"note": "nit: rename"}})

        assert seam.call_args.args == ("acme/widgets",)


class TestReviewSeamRegistration(TestCase):
    def test_cli_import_registers_the_review_service_seam(self) -> None:
        import teatree.cli  # noqa: F401, PLC0415 — the import side-effect under test registers the seam

        # The factory resolves the token for the named repo; stub the resolution so the
        # registration proof does not depend on how the environment is credentialed.
        with patch("teatree.cli.review.service.ReviewService.get_gitlab_token", return_value="tok"):
            seam = review_seam.review_post_seam("acme/widgets")
        assert callable(seam.post_draft_note)
        assert callable(seam.post_comment)

    def test_unregistered_seam_fails_loud(self) -> None:
        original = review_seam._factory
        review_seam.register_review_post_seam(review_seam._unregistered_factory)
        try:
            with pytest.raises(RuntimeError, match="not registered"):
                review_seam.review_post_seam("acme/widgets")
        finally:
            register_review_post_seam(original)


class TestReviewToolsCarryInlineAnchors(TestCase):
    """Inline anchoring is MCP-native — the review doctrine posts one finding per ``path:line``.

    The tools carried no anchor, so every correctly-shaped finding was forced onto the
    containerized CLI (two ~32s process startups per comment on this host). The seam is
    the same gated ``ReviewService``; only the transport changes.
    """

    def test_post_comment_threads_the_inline_anchor(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_comment",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "finding": {"note": "blocker: bug", "anchor": "src/a.py:42"},
                    "live": True,
                },
            )

        assert recorder.calls[0][1] == {
            "repo": "acme/widgets",
            "mr": 7,
            "note": SeamNote(note="blocker: bug", anchor=("src/a.py", 42)),
            "live": True,
        }

    def test_post_draft_note_threads_the_inline_anchor(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_draft_note",
                {"repo": "acme/widgets", "mr": 7, "finding": {"note": "nit", "anchor": "b.py:3"}},
            )

        assert recorder.calls[0][1]["note"].anchor == ("b.py", 3)

    def test_a_blank_anchor_posts_a_general_note(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call("review_post_comment", {"repo": "acme/widgets", "mr": 7, "finding": {"note": "verdict"}})

        assert recorder.calls[0][1]["note"].anchor is None

    def test_a_malformed_anchor_is_refused_before_the_seam(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            # "\u00b2" and "\u0664" both pass ``str.isdigit`` and split ``int()`` two
            # ways: the first RAISES out of the handler, the second parses quietly as 4.
            for anchor in ("b.py", "b.py:", ":3", "b.py:zero", "b.py:0", "b.py:\u00b2", "b.py:\u0664"):
                result = _call(
                    "review_post_comment",
                    {"repo": "acme/widgets", "mr": 7, "finding": {"note": "nit", "anchor": anchor}},
                )
                assert result["code"] == 2, anchor
                assert "path/to/file.py:LINE" in result["message"]

        assert recorder.calls == []

    def test_the_instructions_advertise_the_inline_anchor(self) -> None:
        review_lines = [line for line in write_tools.INSTRUCTIONS.splitlines() if line.startswith("- review_post_")]

        assert len(review_lines) == 3
        assert all("anchor" in line for line in review_lines)

    def test_the_instructions_advertise_the_evidence_record(self) -> None:
        # A tool whose schema takes evidence but whose blurb never says so leaves the
        # agent posting "X is broken" bodies the gate refuses, back on the CLI.
        review_lines = [line for line in write_tools.INSTRUCTIONS.splitlines() if line.startswith("- review_post_")]

        assert all("evidence" in line for line in review_lines)


class TestReviewToolsCarryTheEvidenceRecord(TestCase):
    """The commonest finding shape a review has is the one the gate refuses bare.

    "X is wrong / broken / missing" needs the #1280 receipts, and with no ``evidence``
    on these tools that finding had to go back to the ``t3`` CLI they exist to replace —
    fail-closed, so safe, but it left the batch agents are now told to prefer unable to
    post most of what a review actually says.
    """

    _EVIDENCE = '{"master_check_paths": ["src/a.py:42"], "confidence": "verified"}'

    def test_post_comment_threads_the_evidence_and_the_escapes(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_comment",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "finding": {
                        "note": "HIGH (correctness): the retry is missing from the new path.",
                        "anchor": "src/a.py:42",
                        "evidence": self._EVIDENCE,
                        "allow_bloat": True,
                    },
                },
            )

        posted = recorder.calls[0][1]["note"]
        assert posted.evidence_json == self._EVIDENCE
        assert posted.allow_bloat is True
        assert posted.force_general is False

    def test_post_draft_note_threads_the_evidence(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_draft_note",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "finding": {"note": "the helper is missing", "evidence": self._EVIDENCE},
                },
            )

        assert recorder.calls[0][1]["note"].evidence_json == self._EVIDENCE

    def test_the_batch_carries_evidence_per_comment_not_per_batch(self) -> None:
        recorder = _BatchSeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            _call(
                "review_post_comments",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "comments": [
                        {"note": "the retry is missing", "anchor": "src/a.py:42", "evidence": self._EVIDENCE},
                        {"note": "Nit: rename this helper.", "anchor": "src/b.py:3", "force_general": True},
                    ],
                },
            )

        notes = recorder.calls[0][1]["notes"]
        assert [item.evidence_json for item in notes] == [self._EVIDENCE, ""]
        assert [item.force_general for item in notes] == [False, True]


class _BatchSeamRecorder(_SeamRecorder):
    """A seam that also serves the batch surface."""

    def post_comments(self, repo: str, mr: int, notes: list[SeamNote], *, live: bool = False) -> tuple[str, int]:
        self.calls.append(("batch", {"repo": repo, "mr": mr, "notes": list(notes), "live": live}))
        return ("posted 2", 0)


class TestReviewCommentsPostAsOneBatch(TestCase):
    """One review is N findings, and the authorization ceremony is per-REVIEW, not per-finding.

    Each live comment previously needed its own recorded authorization and its own pair of
    ~32s CLI process starts, so a six-finding review paid the whole dance six times.
    """

    def test_the_batch_threads_every_anchored_note_through_the_seam(self) -> None:
        recorder = _BatchSeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            result = _call(
                "review_post_comments",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "comments": [
                        {"note": "blocker: the retry is gone", "anchor": "src/a.py:42"},
                        {"note": "verdict: two blockers"},
                    ],
                    "live": True,
                },
            )

        assert result["code"] == 0
        assert recorder.calls[0][1]["notes"] == [
            SeamNote(note="blocker: the retry is gone", anchor=("src/a.py", 42)),
            SeamNote(note="verdict: two blockers"),
        ]
        assert recorder.calls[0][1]["live"] is True

    def test_a_malformed_anchor_refuses_the_whole_batch_before_the_seam(self) -> None:
        recorder = _BatchSeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            result = _call(
                "review_post_comments",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "comments": [{"note": "ok", "anchor": "src/a.py:42"}, {"note": "nit", "anchor": "src/b.py:zero"}],
                },
            )

        assert result["code"] == 2
        assert "comment 2" in result["message"]
        assert recorder.calls == []

    def test_a_comment_with_no_note_is_refused(self) -> None:
        recorder = _BatchSeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            result = _call("review_post_comments", {"repo": "acme/widgets", "mr": 7, "comments": [{"anchor": "a:1"}]})

        assert result["code"] == 2
        assert "no 'note'" in result["message"]
        assert recorder.calls == []

    def test_the_instructions_advertise_the_batch(self) -> None:
        review_lines = [line for line in write_tools.INSTRUCTIONS.splitlines() if line.startswith("- review_post_")]

        assert any(line.startswith("- review_post_comments(") for line in review_lines)


class TestEvidenceMayBeTheMappingAModelNaturallyPasses(TestCase):
    """`finding` is a mapping, so `evidence` inside it arrives as one too.

    The tool descriptions call evidence "the #1280 record as JSON", and the surrounding
    keys are real values -- so a mapping is the shape a model passes. `str()` on it
    yields a Python repr with single quotes, which `FindingEvidence.from_json` rejects,
    so the finding class the batch exists to post could not be posted naturally.
    Accepting a mapping must not degrade into accepting anything: a value that is
    neither is refused naming its type, not stringified.
    """

    _MAPPING: ClassVar[dict[str, Any]] = {
        "master_check_paths": ["src/teatree/cli/review/service.py:42"],
        "confidence": "verified",
    }
    _STRING = '{"master_check_paths": ["src/teatree/cli/review/service.py:42"], "confidence": "verified"}'

    def test_a_mapping_round_trips_through_the_single_evidence_parser(self) -> None:
        built = review_write_tools._seam_note({"note": "the retry is missing", "evidence": self._MAPPING})

        assert isinstance(built, SeamNote)
        assert FindingEvidence.from_json(built.evidence_json) == FindingEvidence(
            master_check_paths=["src/teatree/cli/review/service.py:42"], confidence="verified"
        )

    def test_a_string_passes_through_byte_identical(self) -> None:
        built = review_write_tools._seam_note({"note": "the retry is missing", "evidence": self._STRING})

        assert isinstance(built, SeamNote)
        assert built.evidence_json == self._STRING

    def test_an_absent_or_empty_evidence_stays_empty(self) -> None:
        for finding in ({"note": "nit"}, {"note": "nit", "evidence": ""}, {"note": "nit", "evidence": None}):
            built = review_write_tools._seam_note(finding)
            assert isinstance(built, SeamNote), finding
            assert built.evidence_json == "", finding

    def test_a_value_that_is_neither_a_mapping_nor_a_string_is_refused_naming_its_type(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            for evidence, type_name in ((42, "int"), (["src/a.py:42"], "list"), (True, "bool")):
                result = _call(
                    "review_post_comment",
                    {"repo": "acme/widgets", "mr": 7, "finding": {"note": "nit", "evidence": evidence}},
                )
                assert result["code"] == 2, evidence
                assert type_name in result["message"], (evidence, result["message"])
                assert "must be a JSON object" in result["message"], (evidence, result["message"])

        assert recorder.calls == []

    def test_a_mapping_json_cannot_serialise_is_refused_naming_the_reason_not_its_type(self) -> None:
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            result = _call(
                "review_post_comment",
                {
                    "repo": "acme/widgets",
                    "mr": 7,
                    "finding": {"note": "nit", "evidence": {"confidence": "verified", "checked_at": _NOW}},
                },
            )

        assert result["code"] == 2, result
        assert "serialis" in result["message"], result["message"]
        assert "datetime" in result["message"], result["message"]
        assert "must be a JSON object" not in result["message"], result["message"]
        assert recorder.calls == []

    def test_a_self_referential_mapping_is_refused_rather_than_crashing_the_tool(self) -> None:
        looping: dict[str, Any] = {"confidence": "verified"}
        looping["self"] = looping
        recorder = _SeamRecorder()
        with patch("teatree.mcp.review_write_tools.review_post_seam", return_value=recorder):
            result = _call(
                "review_post_comment",
                {"repo": "acme/widgets", "mr": 7, "finding": {"note": "nit", "evidence": looping}},
            )

        assert result["code"] == 2, result
        assert "serialis" in result["message"], result["message"]
        assert "Circular reference" in result["message"], result["message"]
        assert recorder.calls == []

    def test_a_mapping_carrying_a_bad_confidence_reaches_the_parser_that_refuses_it(self) -> None:
        built = review_write_tools._seam_note({"note": "nit", "evidence": {"confidence": "bogus"}})

        assert isinstance(built, SeamNote)
        with pytest.raises(ValueError, match="confidence"):
            FindingEvidence.from_json(built.evidence_json)

    def test_the_tool_descriptions_name_both_accepted_evidence_shapes(self) -> None:
        review_lines = [line for line in write_tools.INSTRUCTIONS.splitlines() if line.startswith("- review_post_")]

        assert len(review_lines) == 3
        assert all("JSON object or its JSON string" in line for line in review_lines)
