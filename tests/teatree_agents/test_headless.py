import json
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.headless as headless_mod
from teatree.agents.headless import (
    _get_resume_session_id,
    _parse_cli_envelope,
    _parse_result,
    _safe_float,
    _safe_int,
    _validate_result,
    get_result_json_schema,
    run_headless,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket


class TestRunHeadless(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_captures_structured_result(self) -> None:
        result_json = json.dumps({"summary": "Done", "tests_passed": 5, "tests_failed": 0})
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                headless_mod.subprocess,
                "run",
                return_value=CompletedProcess([], 0, f"Progress...\n{result_json}\n", ""),
            ),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["summary"] == "Done"
        assert attempt.result["tests_passed"] == 5
        assert task.status == Task.Status.COMPLETED

    def test_records_failure(self) -> None:
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(headless_mod.subprocess, "run", return_value=CompletedProcess([], 1, "", "segfault")),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 1
        assert attempt.error == "segfault"
        assert task.status == Task.Status.FAILED

    def test_fails_when_binary_not_found(self) -> None:
        with patch.object(headless_mod.shutil, "which", return_value=None):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "not installed" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_fails_when_no_json_in_successful_exit(self) -> None:
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                headless_mod.subprocess,
                "run",
                return_value=CompletedProcess([], 0, "no structured output\n", ""),
            ),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert "no structured output" in attempt.result["summary"]
        assert task.status == Task.Status.COMPLETED

    def test_fails_when_result_violates_schema(self) -> None:
        bad_json = json.dumps({"summary": "OK", "rogue_field": True})
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(headless_mod.subprocess, "run", return_value=CompletedProcess([], 0, f"{bad_json}\n", "")),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert "unexpected keys" in attempt.error
        assert "rogue_field" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_routes_to_interactive_when_needs_user_input(self) -> None:
        result_json = json.dumps(
            {
                "summary": "Blocked on design",
                "needs_user_input": True,
                "user_input_reason": "Need design decision",
            }
        )
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(headless_mod.subprocess, "run", return_value=CompletedProcess([], 0, f"{result_json}\n", "")),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
            task = Task.objects.create(
                ticket=self.ticket, session=session, execution_target=Task.ExecutionTarget.HEADLESS
            )

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["needs_user_input"] is True
        # Task completes normally; a new interactive task is created for follow-up
        assert task.status == Task.Status.COMPLETED
        followup = Task.objects.filter(
            ticket=self.ticket,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
        ).first()
        assert followup is not None
        assert "Need design decision" in followup.execution_reason

    def test_parses_cli_envelope_with_session_id(self) -> None:
        """When the CLI returns a JSON envelope, session_id is extracted and stored."""
        result_json = json.dumps({"summary": "Work done"})
        cli_envelope = json.dumps({"session_id": "sess-abc-123", "result": result_json})

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(headless_mod.subprocess, "run", return_value=CompletedProcess([], 0, cli_envelope, "")),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        assert attempt.exit_code == 0
        assert attempt.agent_session_id == "sess-abc-123"
        assert attempt.result["summary"] == "Work done"


# --- Pure function tests (no DB) ---


def test_validate_result_accepts_valid_keys() -> None:
    assert _validate_result({"summary": "OK", "tests_passed": 5}) == ""


def test_validate_result_rejects_unknown_keys() -> None:
    error = _validate_result({"summary": "OK", "bogus": True})
    assert "bogus" in error


def test_parse_result_extracts_last_json_line() -> None:
    stdout = "Loading skills...\nRunning task...\n" + json.dumps({"summary": "OK"}) + "\n"
    assert _parse_result(stdout) == {"summary": "OK"}


def test_parse_result_returns_empty_dict_for_no_json() -> None:
    assert _parse_result("no json here\n") == {}


def test_parse_result_skips_malformed_json() -> None:
    assert _parse_result("{bad json\n") == {}


def test_get_result_json_schema_returns_valid_schema() -> None:
    schema = get_result_json_schema()
    assert schema["type"] == "object"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "summary" in properties


# --- Session resume tests ---

FAKE_SESSION_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestGetResumeSessionId(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_from_parent_attempt(self) -> None:
        """Parent task's attempt has an agent_session_id — headless should resume it."""
        parent_session = Session.objects.create(ticket=self.ticket, agent_id="interactive-followup")
        parent_task = Task.objects.create(ticket=self.ticket, session=parent_session)
        TaskAttempt.objects.create(task=parent_task, agent_session_id=FAKE_SESSION_UUID)

        child_session = Session.objects.create(ticket=self.ticket, agent_id="coding")
        child_task = Task.objects.create(ticket=self.ticket, session=child_session, parent_task=parent_task)

        assert _get_resume_session_id(child_task) == FAKE_SESSION_UUID

    def test_from_parent_session_agent_id(self) -> None:
        """Parent task's session.agent_id is a UUID — headless should resume it.

        This is the case when headless->interactive carried the session_id on
        Session.agent_id but the interactive TaskAttempt has no agent_session_id.
        """
        parent_session = Session.objects.create(ticket=self.ticket, agent_id=FAKE_SESSION_UUID)
        parent_task = Task.objects.create(ticket=self.ticket, session=parent_session)

        child_session = Session.objects.create(ticket=self.ticket, agent_id="review")
        child_task = Task.objects.create(ticket=self.ticket, session=child_session, parent_task=parent_task)

        assert _get_resume_session_id(child_task) == FAKE_SESSION_UUID

    def test_returns_empty_without_parent(self) -> None:
        """No parent task — nothing to resume."""
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert _get_resume_session_id(task) == ""

    def test_skips_non_uuid_agent_ids(self) -> None:
        """Parent exists but agent_id is not a UUID — don't resume."""
        parent_session = Session.objects.create(ticket=self.ticket, agent_id="not-a-uuid")
        parent_task = Task.objects.create(ticket=self.ticket, session=parent_session)

        child_session = Session.objects.create(ticket=self.ticket, agent_id="coding")
        child_task = Task.objects.create(ticket=self.ticket, session=child_session, parent_task=parent_task)

        assert _get_resume_session_id(child_task) == ""


class TestRunHeadlessResumesParentSession(TestCase):
    def test_resumes_parent_session(self) -> None:
        """When a parent task has a session_id, run_headless passes --resume to the CLI."""
        captured_commands: list[list[str]] = []
        result_json = json.dumps({"summary": "Continued work"})

        def fake_run(*args: object, **_kwargs: object) -> CompletedProcess[str]:
            captured_commands.append(list(args[0]))
            return CompletedProcess([], 0, f"{result_json}\n", "")

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(headless_mod.subprocess, "run", side_effect=fake_run),
        ):
            ticket = Ticket.objects.create()
            parent_session = Session.objects.create(ticket=ticket, agent_id=FAKE_SESSION_UUID)
            parent_task = Task.objects.create(ticket=ticket, session=parent_session)

            child_session = Session.objects.create(ticket=ticket, agent_id="coding")
            child_task = Task.objects.create(ticket=ticket, session=child_session, parent_task=parent_task)

            run_headless(child_task, phase="coding", overlay_skill_metadata={})

        cmd = captured_commands[0]
        assert "--resume" in cmd
        resume_idx = cmd.index("--resume")
        assert cmd[resume_idx + 1] == FAKE_SESSION_UUID


# --- _safe_int / _safe_float ---


def test_safe_int_converts_string() -> None:
    assert _safe_int("42") == 42
    assert _safe_int("3.7") == 3  # truncates


def test_safe_int_returns_none_for_invalid() -> None:
    assert _safe_int(None) is None
    assert _safe_int("abc") is None


def test_safe_float_converts_string() -> None:
    assert _safe_float("0.042") == pytest.approx(0.042)


def test_safe_float_returns_none_for_invalid() -> None:
    assert _safe_float(None) is None
    assert _safe_float("abc") is None


# --- _parse_cli_envelope ---


def test_parse_cli_envelope_extracts_session_id_and_result() -> None:
    envelope = json.dumps({"session_id": "abc-123", "result": "Agent output text"})
    parsed = _parse_cli_envelope(envelope)
    assert parsed["session_id"] == "abc-123"
    assert parsed["agent_text"] == "Agent output text"


def test_parse_cli_envelope_extracts_usage_stats() -> None:
    envelope = json.dumps(
        {
            "session_id": "abc-123",
            "result": "Done",
            "input_tokens": 5000,
            "output_tokens": 1200,
            "cost_usd": 0.042,
            "num_turns": 3,
        }
    )
    parsed = _parse_cli_envelope(envelope)
    assert parsed["input_tokens"] == "5000"
    assert parsed["output_tokens"] == "1200"
    assert parsed["cost_usd"] == "0.042"
    assert parsed["num_turns"] == "3"


def test_parse_cli_envelope_omits_missing_usage_stats() -> None:
    envelope = json.dumps({"session_id": "abc-123", "result": "Done"})
    parsed = _parse_cli_envelope(envelope)
    assert "input_tokens" not in parsed
    assert "cost_usd" not in parsed


def test_parse_cli_envelope_falls_back_for_non_envelope_json() -> None:
    parsed = _parse_cli_envelope('{"summary": "OK"}')
    assert parsed["agent_text"] == '{"summary": "OK"}'
    assert parsed["session_id"] == ""


def test_parse_cli_envelope_falls_back_for_invalid_json() -> None:
    parsed = _parse_cli_envelope("not json at all")
    assert parsed["agent_text"] == "not json at all"
    assert parsed["session_id"] == ""
