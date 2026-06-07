import contextlib
import json
import shlex
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase, override_settings

import teatree.agents.headless as headless_mod
from teatree.agents.headless import (
    LoopWatchdog,
    TicketBudget,
    _get_resume_session_id,
    _parse_cli_envelope,
    _parse_result,
    _run_with_heartbeat,
    _safe_float,
    _safe_int,
    _validate_result,
    get_result_json_schema,
    run_headless,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket


@contextlib.contextmanager
def _fake_claude(stdout: str = "", stderr: str = "", exit_code: int = 0) -> Iterator[None]:
    """Run ``run_headless`` against a real ``sh -c`` subprocess.

    The headless runner now drives the agent over ``Popen`` so the
    heartbeat loop can terminate a runaway. Tests exercise that real
    transport against a harmless shell command instead of mocking the
    subprocess layer.
    """
    script = f"printf %s {shlex.quote(stdout)}; printf %s {shlex.quote(stderr)} >&2; exit {exit_code}"
    with (
        patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(headless_mod, "_build_headless_command", return_value=["sh", "-c", script]),
    ):
        yield


class TestRunHeadless(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_captures_structured_result(self) -> None:
        # #1284: ``coding`` phase requires ``files_modified`` evidence — the
        # agent's claim that it shipped code must back the claim with an
        # actual file change record.
        result_json = json.dumps(
            {
                "summary": "Done",
                "files_modified": [{"path": "src/x.py", "action": "modified"}],
                "tests_passed": 5,
                "tests_failed": 0,
            },
        )
        with _fake_claude(stdout=f"Progress...\n{result_json}\n"):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["summary"] == "Done"
        assert attempt.result["tests_passed"] == 5
        assert task.status == Task.Status.COMPLETED

    def test_records_failure(self) -> None:
        with _fake_claude(stderr="segfault", exit_code=1):
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

    def test_summary_fallback_on_no_json_for_phase_without_evidence_requirement(self) -> None:
        # #1284: when the agent emits no JSON, the fallback ``summary``
        # capture path still completes the task IF the phase has no
        # evidence requirement (``scoping``/``retro``). The "coding" case
        # — where evidence IS required — is covered by
        # ``test_no_json_fails_phase_with_evidence_requirement``.
        with _fake_claude(stdout="no structured output\n"):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="scoping", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert "no structured output" in attempt.result["summary"]
        assert task.status == Task.Status.COMPLETED

    def test_no_json_fails_phase_with_evidence_requirement(self) -> None:
        # #1284 (codex #1282-6): the no-JSON fallback produces only a
        # ``summary`` blob. On a phase that requires concrete evidence
        # (``coding`` → ``files_modified``), that summary is not enough —
        # the attempt must be recorded as failed so the agent retries
        # with real evidence rather than silently advancing the FSM.
        with _fake_claude(stdout="no structured output\n"):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert "files_modified" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_fails_when_result_violates_schema(self) -> None:
        bad_json = json.dumps({"summary": "OK", "rogue_field": True})
        with _fake_claude(stdout=f"{bad_json}\n"):
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
            },
        )
        with _fake_claude(stdout=f"{result_json}\n"):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
            task = Task.objects.create(
                ticket=self.ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
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
        result_json = json.dumps(
            {
                "summary": "Work done",
                "files_modified": [{"path": "src/x.py", "action": "modified"}],
            },
        )
        cli_envelope = json.dumps({"session_id": "sess-abc-123", "result": result_json})

        with _fake_claude(stdout=cli_envelope):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        assert attempt.exit_code == 0
        assert attempt.agent_session_id == "sess-abc-123"
        assert attempt.result["summary"] == "Work done"

    def test_persists_usage_and_cost_from_real_envelope(self) -> None:
        """Captured tokens, model, and ``total_cost_usd`` land on the row.

        The regression this guards: ``cost_usd`` was NULL on every attempt.
        """
        result_json = json.dumps(
            {"summary": "Work done", "files_modified": [{"path": "src/x.py", "action": "modified"}]},
        )
        cli_envelope = json.dumps(
            {
                "session_id": "sess-xyz",
                "result": result_json,
                "num_turns": 4,
                "total_cost_usd": 0.5,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 9000,
                },
                "modelUsage": {"claude-opus-4-8[1m]": {"costUSD": 0.5}},
            },
        )
        with _fake_claude(stdout=cli_envelope):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        attempt.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.cost_usd == pytest.approx(0.5)
        assert attempt.input_tokens == 1000
        assert attempt.output_tokens == 200
        assert attempt.cache_write_tokens == 50
        assert attempt.cache_read_tokens == 9000
        assert attempt.model == "claude-opus-4-8[1m]"
        assert attempt.num_turns == 4

    def test_estimates_cost_from_price_table_when_cli_cost_absent(self) -> None:
        """An envelope with tokens but no ``total_cost_usd`` still persists a cost.

        The price-table estimate is stored so historical rows get one too.
        """
        result_json = json.dumps(
            {"summary": "Work done", "files_modified": [{"path": "src/x.py", "action": "modified"}]},
        )
        cli_envelope = json.dumps(
            {
                "session_id": "sess-noc",
                "result": result_json,
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
                "modelUsage": {"claude-sonnet-4-6": {}},
            },
        )
        with _fake_claude(stdout=cli_envelope):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        attempt.refresh_from_db()
        assert attempt.exit_code == 0
        # 1M input tokens at the Sonnet $3/MTok input rate.
        assert attempt.cost_usd == pytest.approx(3.0)


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
        real = headless_mod._build_headless_command

        def spy_build(*args: object, **kwargs: object) -> list[str]:
            captured_commands.append(real(*args, **kwargs))
            return ["sh", "-c", f"printf %s {shlex.quote(result_json)}"]

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(headless_mod, "_build_headless_command", side_effect=spy_build),
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


def test_parse_cli_envelope_extracts_real_nested_usage_and_total_cost() -> None:
    """Nested ``usage`` tokens and ``total_cost_usd`` are extracted.

    The old flat-key reader captured nothing — cost_usd was NULL on every row.
    """
    envelope = json.dumps(
        {
            "session_id": "abc-123",
            "result": "Done",
            "num_turns": 3,
            "total_cost_usd": 0.087722,
            "usage": {
                "input_tokens": 10817,
                "output_tokens": 5,
                "cache_creation_input_tokens": 3904,
                "cache_read_input_tokens": 18224,
            },
            "modelUsage": {"claude-opus-4-8[1m]": {"costUSD": 0.087722}},
        },
    )
    parsed = _parse_cli_envelope(envelope)
    assert parsed["input_tokens"] == "10817"
    assert parsed["output_tokens"] == "5"
    assert parsed["cache_write_tokens"] == "3904"
    assert parsed["cache_read_tokens"] == "18224"
    assert parsed["cost_usd"] == "0.087722"
    assert parsed["num_turns"] == "3"
    assert parsed["model"] == "claude-opus-4-8[1m]"


def test_parse_cli_envelope_honours_pre_2x_flat_usage_fallback() -> None:
    envelope = json.dumps(
        {
            "session_id": "abc-123",
            "result": "Done",
            "input_tokens": 5000,
            "output_tokens": 1200,
            "cost_usd": 0.042,
            "num_turns": 3,
        },
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
    assert "model" not in parsed


def test_parse_cli_envelope_falls_back_for_non_envelope_json() -> None:
    parsed = _parse_cli_envelope('{"summary": "OK"}')
    assert parsed["agent_text"] == '{"summary": "OK"}'
    assert parsed["session_id"] == ""


def test_parse_cli_envelope_falls_back_for_invalid_json() -> None:
    parsed = _parse_cli_envelope("not json at all")
    assert parsed["agent_text"] == "not json at all"
    assert parsed["session_id"] == ""


# --- _run_with_heartbeat tests ---


class TestRunWithHeartbeat(TestCase):
    def test_calls_renew_lease(self) -> None:
        """Heartbeat thread calls renew_lease() while the subprocess runs."""
        renew_count = 0

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        def counting_renew(**_kwargs: object) -> None:
            nonlocal renew_count
            renew_count += 1

        task.renew_lease = counting_renew

        off = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        with patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05):
            stdout, _stderr, returncode = _run_with_heartbeat(
                task,
                ["sh", "-c", "sleep 0.4; printf done"],
                watchdog=off,
            )

        assert returncode == 0
        assert stdout == "done"
        assert renew_count >= 2

    def test_stops_after_subprocess_completes(self) -> None:
        """Heartbeat thread stops cleanly after subprocess exits."""
        renew_calls: list[float] = []

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        def tracking_renew(**_kwargs: object) -> None:
            renew_calls.append(time.monotonic())

        task.renew_lease = tracking_renew

        off = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        with patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05):
            _run_with_heartbeat(task, ["sh", "-c", "exit 0"], watchdog=off)

        count_at_exit = len(renew_calls)
        time.sleep(0.15)
        assert len(renew_calls) == count_at_exit

    def test_survives_renew_lease_failure(self) -> None:
        """A failing renew_lease() is logged but doesn't crash the subprocess."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        def failing_renew(**_kwargs: object) -> None:
            msg = "DB connection lost"
            raise RuntimeError(msg)

        task.renew_lease = failing_renew

        off = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        with (
            patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05),
            patch.object(headless_mod, "logger") as mock_logger,
        ):
            stdout, _stderr, returncode = _run_with_heartbeat(
                task,
                ["sh", "-c", "sleep 0.15; printf ok"],
                watchdog=off,
            )

        assert returncode == 0
        assert stdout == "ok"
        assert mock_logger.warning.call_count >= 1


# --- Stuck-loop / cost-spike watchdog (#882) ---


class TestLoopWatchdog(TestCase):
    """Watchdog evaluation against real Task / TaskAttempt rows."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_disabled_watchdog_never_terminates(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        assert watchdog.breach_reason(self.task, elapsed_seconds=99999) is None

    def test_runtime_ceiling_breach(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0)
        assert watchdog.breach_reason(self.task, elapsed_seconds=10) is None
        reason = watchdog.breach_reason(self.task, elapsed_seconds=31)
        assert reason is not None
        assert "runtime" in reason
        assert "31" in reason

    def test_turn_count_breach_from_accumulated_attempts(self) -> None:
        TaskAttempt.objects.create(task=self.task, num_turns=120)
        TaskAttempt.objects.create(task=self.task, num_turns=140)
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=200, max_cost_usd=0.0)
        reason = watchdog.breach_reason(self.task, elapsed_seconds=5)
        assert reason is not None
        assert "turns" in reason
        assert "260" in reason

    def test_cost_breach_from_accumulated_attempts(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=4.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.5)
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=5.0)
        reason = watchdog.breach_reason(self.task, elapsed_seconds=5)
        assert reason is not None
        assert "cost" in reason
        assert "7.5" in reason

    def test_under_all_thresholds_no_breach(self) -> None:
        TaskAttempt.objects.create(task=self.task, num_turns=10, cost_usd=0.5)
        watchdog = LoopWatchdog(max_runtime_seconds=600, max_turns=200, max_cost_usd=5.0)
        assert watchdog.breach_reason(self.task, elapsed_seconds=60) is None

    def test_from_settings_reads_defaults(self) -> None:
        with override_settings(
            TEATREE_LOOP_WATCHDOG={"max_runtime_seconds": 42, "max_turns": 7, "max_cost_usd": 1.5},
        ):
            watchdog = LoopWatchdog.from_settings()
        assert watchdog.max_runtime_seconds == 42
        assert watchdog.max_turns == 7
        assert watchdog.max_cost_usd == pytest.approx(1.5)

    def test_from_settings_falls_back_to_conservative_default(self) -> None:
        with override_settings():
            from django.conf import settings  # noqa: PLC0415

            if hasattr(settings, "TEATREE_LOOP_WATCHDOG"):
                del settings.TEATREE_LOOP_WATCHDOG
            watchdog = LoopWatchdog.from_settings()
        # Conservative documented default: a generous runtime ceiling that
        # only trips on genuine runaways; turn/cost ceilings off by default
        # (absolute budget caps are #398-4's job, not the watchdog's).
        assert watchdog.max_runtime_seconds > 0
        assert watchdog.max_turns == 0
        assert watchdog.max_cost_usd == pytest.approx(0.0)


class TestRunWithHeartbeatWatchdog(TestCase):
    """A spinning subprocess is killed and a stuck_loop failure recorded."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)
        # Sibling heartbeat tests stub renew_lease to keep the heartbeat
        # thread off the DB — threaded ORM access under TestCase's wrapping
        # transaction is a test-harness artifact, not production behaviour.
        self.task.renew_lease = lambda **_kw: None

    def test_kills_runaway_subprocess_on_runtime_breach(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=0.2, max_turns=0, max_cost_usd=0.0)
        start = time.monotonic()
        with patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05):
            _stdout, stderr, returncode = _run_with_heartbeat(
                self.task,
                ["sleep", "30"],
                watchdog=watchdog,
            )
        elapsed = time.monotonic() - start

        assert returncode != 0
        assert elapsed < 10  # killed early, not after 30s
        assert "stuck_loop" in stderr
        assert "runtime" in stderr

    def test_normal_subprocess_not_killed(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0)
        with patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05):
            stdout, _stderr, returncode = _run_with_heartbeat(
                self.task,
                ["sh", "-c", "printf done"],
                watchdog=watchdog,
            )
        assert returncode == 0
        assert stdout == "done"


class TestWatchdogGracefulTermination(TestCase):
    """Watchdog SIGTERMs first, grants a grace window, SIGKILLs only if needed (#997)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)
        self.task.renew_lease = lambda **_kw: None

    def test_subprocess_handles_sigterm_and_is_not_sigkilled(self) -> None:
        marker = Path(tempfile.mkdtemp()) / "term-handled"
        # Traps SIGTERM, writes the marker (its "flush final status" stand-in),
        # then exits during the grace window. A correct watchdog never escalates
        # to SIGKILL here; an immediate SIGKILL would skip the trap entirely.
        script = f"trap 'printf done > {shlex.quote(str(marker))}; exit 0' TERM; while :; do sleep 0.05; done"
        watchdog = LoopWatchdog(max_runtime_seconds=0.2, max_turns=0, max_cost_usd=0.0)
        with (
            patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05),
            patch.object(headless_mod, "_WATCHDOG_TERM_GRACE_SECONDS", 5.0),
        ):
            _stdout, stderr, returncode = _run_with_heartbeat(
                self.task,
                ["sh", "-c", script],
                watchdog=watchdog,
            )

        assert marker.exists(), "SIGTERM handler never ran — process was hard-killed without a grace window"
        assert returncode != 0
        assert "stuck_loop" in stderr

    def test_subprocess_ignoring_sigterm_is_sigkilled_after_grace(self) -> None:
        # Ignores SIGTERM and keeps running; the watchdog must escalate to
        # SIGKILL once the grace window elapses, bounded in time.
        script = "trap '' TERM; while :; do sleep 0.05; done"
        watchdog = LoopWatchdog(max_runtime_seconds=0.2, max_turns=0, max_cost_usd=0.0)
        start = time.monotonic()
        with (
            patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05),
            patch.object(headless_mod, "_WATCHDOG_TERM_GRACE_SECONDS", 0.5),
        ):
            _stdout, stderr, returncode = _run_with_heartbeat(
                self.task,
                ["sh", "-c", script],
                watchdog=watchdog,
            )
        elapsed = time.monotonic() - start

        assert returncode != 0
        assert elapsed < 10  # escalation is bounded, not a hang
        assert "stuck_loop" in stderr


class TestRunHeadlessRecordsStuckLoop(TestCase):
    """run_headless records a stuck_loop TaskAttempt failure when the watchdog fires."""

    def test_records_stuck_loop_failure_with_observed_deltas(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task, num_turns=500)
        task.renew_lease = lambda **_kw: None

        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=200, max_cost_usd=0.0)
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(headless_mod.LoopWatchdog, "from_settings", return_value=watchdog),
            patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.05),
            patch.object(headless_mod, "_build_headless_command", return_value=["sleep", "30"]),
        ):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert "stuck_loop" in attempt.error
        assert "turns" in attempt.error
        assert "500" in attempt.error
        assert task.status == Task.Status.FAILED


class TestTicketBudget(TestCase):
    """Per-ticket cumulative cost-cap consumer (#885 / #398-4).

    Sums ``TaskAttempt.cost_usd`` across *every* task under the ticket
    (not just the task being dispatched, unlike the per-task
    ``LoopWatchdog``) and refuses further dispatch once the configured
    per-ticket ceiling is crossed.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_disabled_budget_never_refuses(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=9999.0)
        budget = TicketBudget(max_cost_usd=0.0)
        assert budget.breach_reason(self.ticket) is None

    def test_under_cap_does_not_refuse(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=2.0)
        budget = TicketBudget(max_cost_usd=5.0)
        assert budget.breach_reason(self.ticket) is None

    def test_over_cap_refuses_with_observed_total(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=4.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.5)
        budget = TicketBudget(max_cost_usd=5.0)
        reason = budget.breach_reason(self.ticket)
        assert reason is not None
        assert "budget" in reason
        assert "7.50" in reason
        assert "5.00" in reason

    def test_sums_across_all_tasks_of_the_ticket(self) -> None:
        other_session = Session.objects.create(ticket=self.ticket)
        other_task = Task.objects.create(ticket=self.ticket, session=other_session)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.0)
        TaskAttempt.objects.create(task=other_task, cost_usd=3.0)
        budget = TicketBudget(max_cost_usd=5.0)
        reason = budget.breach_reason(self.ticket)
        assert reason is not None
        assert "6.00" in reason

    def test_ignores_other_tickets(self) -> None:
        other_ticket = Ticket.objects.create()
        other_session = Session.objects.create(ticket=other_ticket)
        other_task = Task.objects.create(ticket=other_ticket, session=other_session)
        TaskAttempt.objects.create(task=other_task, cost_usd=99.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=1.0)
        budget = TicketBudget(max_cost_usd=5.0)
        assert budget.breach_reason(self.ticket) is None

    def test_from_settings_reads_configured_cap(self) -> None:
        with override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 12.5}):
            budget = TicketBudget.from_settings()
        assert budget.max_cost_usd == pytest.approx(12.5)

    def test_from_settings_defaults_to_disabled(self) -> None:
        with override_settings():
            from django.conf import settings  # noqa: PLC0415

            if hasattr(settings, "TEATREE_TICKET_BUDGET"):
                del settings.TEATREE_TICKET_BUDGET
            budget = TicketBudget.from_settings()
        # Conservative documented default mirrors #882's precedent: the
        # cap is opt-in (0.0 = disabled) so no behaviour change until the
        # user configures a ceiling.
        assert budget.max_cost_usd == pytest.approx(0.0)


class TestRunHeadlessRefusesOverBudgetTicket(TestCase):
    """run_headless refuses dispatch and records a budget_exceeded failure."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")

    def test_over_budget_ticket_is_not_dispatched(self) -> None:
        spent = Task.objects.create(ticket=self.ticket, session=self.session)
        TaskAttempt.objects.create(task=spent, cost_usd=8.0)
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        with (
            override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 5.0}),
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(
                headless_mod,
                "_build_headless_command",
                side_effect=AssertionError("subprocess must not be launched over budget"),
            ),
        ):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert "budget_exceeded" in attempt.error
        assert "8.00" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_under_budget_ticket_proceeds(self) -> None:
        spent = Task.objects.create(ticket=self.ticket, session=self.session)
        TaskAttempt.objects.create(task=spent, cost_usd=1.0)
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        result_json = json.dumps(
            {
                "summary": "Done",
                "files_modified": [{"path": "src/x.py", "action": "modified"}],
            },
        )

        with override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 5.0}), _fake_claude(stdout=f"{result_json}\n"):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert task.status == Task.Status.COMPLETED


# --- _build_headless_command model tiering (#880) ---


def test_build_command_omits_model_flag_by_default() -> None:
    """Without a resolved model, no --model flag is appended (inherit default)."""
    cmd = headless_mod._build_headless_command("/bin/claude", "p", "ctx")
    assert "--model" not in cmd


def test_build_command_appends_model_flag_when_set() -> None:
    """A resolved model tier is passed to the Claude CLI via --model."""
    cmd = headless_mod._build_headless_command("/bin/claude", "p", "ctx", model="sonnet")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_build_command_empty_model_omits_flag() -> None:
    """An empty model string means inherit the user's default — no flag."""
    cmd = headless_mod._build_headless_command("/bin/claude", "p", "ctx", model="")
    assert "--model" not in cmd


class TestRunHeadlessModelTiering(TestCase):
    """Mechanical phases invoke claude with a cheap tier; reasoning phases inherit the default."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _run_capturing_command(self, phase: str) -> list[str]:
        result_json = json.dumps({"summary": "Done"})
        captured: dict[str, list[str]] = {}
        real = headless_mod._build_headless_command

        def spy_build(*args: object, **kwargs: object) -> list[str]:
            captured["command"] = real(*args, **kwargs)
            return ["sh", "-c", f"printf %s {shlex.quote(result_json)}"]

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(headless_mod, "_build_headless_command", side_effect=spy_build),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            run_headless(task, phase=phase, overlay_skill_metadata={})

        return captured["command"]

    def test_retrospecting_runs_on_haiku(self) -> None:
        command = self._run_capturing_command("retrospecting")
        assert "--model" in command
        assert command[command.index("--model") + 1] == "haiku"

    def test_reviewing_runs_on_sonnet(self) -> None:
        command = self._run_capturing_command("reviewing")
        assert "--model" in command
        assert command[command.index("--model") + 1] == "sonnet"

    def test_coding_inherits_user_default_model(self) -> None:
        command = self._run_capturing_command("coding")
        assert "--model" not in command
