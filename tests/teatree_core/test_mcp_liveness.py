"""The MCP liveness check must be able to FAIL — on each of the three real causes.

A check for a dead MCP server that cannot itself go red is the exact irony it exists
to prevent, so every branch of :func:`classify_exercise` is driven here from a captured
run, plus the false green that made this take a whole session: a server that ANSWERS
``initialize`` (so ``claude mcp list`` reports connected) while every ORM-backed tool
call fails on ``unable to open database file``.
"""

import json

import pytest

from teatree.core.mcp_liveness import (
    HANDSHAKE_BUDGET_SECONDS,
    ExerciseRun,
    McpFailureCause,
    classify_exercise,
    exercise_mcp_server,
)

_GOOD_STDOUT = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})

_IMPORT_ERROR_STDERR = (
    'File "/x/src/teatree/mcp/server.py", line 27, in <module>\n'
    "    from mcp.server.mcpserver import MCPServer\n"
    "ModuleNotFoundError: No module named 'mcp.server.mcpserver'\n"
)
_DB_ERROR_STDERR = (
    '  File "django/db/backends/sqlite3/base.py", line 205, in get_new_connection\n'
    "django.db.utils.OperationalError: unable to open database file\n"
)


def _run(*, stdout: str = "", stderr: str = "", elapsed: float = 1.0, timed_out: bool = False) -> ExerciseRun:
    return ExerciseRun(stdout=stdout, stderr=stderr, elapsed=elapsed, timed_out=timed_out)


class TestEachCauseIsDistinguished:
    @pytest.mark.parametrize(
        ("run", "expected"),
        [
            pytest.param(
                _run(stderr=_IMPORT_ERROR_STDERR),
                McpFailureCause.STALE_TOOL_ENV,
                id="stale-tool-env-dies-on-import",
            ),
            pytest.param(
                _run(stderr=_DB_ERROR_STDERR),
                McpFailureCause.DELEGATION_FAILURE,
                id="delegation-failure-cannot-open-the-db",
            ),
            pytest.param(
                _run(elapsed=HANDSHAKE_BUDGET_SECONDS * 3, timed_out=True),
                McpFailureCause.SLOW_STARTUP,
                id="startup-past-the-handshake-budget",
            ),
            pytest.param(
                _run(stderr="something nobody has seen before\n"),
                McpFailureCause.NO_RESPONSE,
                id="no-response-and-no-known-signature",
            ),
        ],
    )
    def test_the_cause_is_named(self, run: ExerciseRun, expected: McpFailureCause) -> None:
        outcome = classify_exercise(run)

        assert outcome.ok is False
        assert outcome.cause is expected

    @pytest.mark.parametrize(
        ("run", "expected"),
        [
            pytest.param(_run(stderr=_IMPORT_ERROR_STDERR), "uv tool install --editable", id="stale-tool-env"),
            pytest.param(_run(stderr=_DB_ERROR_STDERR), "delegation failure", id="delegation-failure"),
            pytest.param(_run(elapsed=99.0, timed_out=True), "AppConfig.ready()", id="slow-startup"),
        ],
    )
    def test_the_finding_carries_the_concrete_remedy(self, run: ExerciseRun, expected: str) -> None:
        assert expected in classify_exercise(run).finding


class TestTheFalseGreen:
    """A handshake that succeeds is not evidence the server works."""

    def test_a_connected_server_whose_db_is_unreachable_is_a_failure(self) -> None:
        outcome = classify_exercise(_run(stdout=_GOOD_STDOUT, stderr=_DB_ERROR_STDERR))

        assert outcome.ok is False
        assert outcome.cause is McpFailureCause.DELEGATION_FAILURE
        assert "can present as CONNECTED" in outcome.finding

    def test_the_captured_stderr_survives_into_the_outcome(self) -> None:
        """`claude mcp list` only ever says `Connection closed`; the trace is the deliverable."""
        assert "unable to open database file" in classify_exercise(_run(stderr=_DB_ERROR_STDERR)).stderr_excerpt


class TestTheBudget:
    def test_a_slow_but_successful_handshake_still_fails(self) -> None:
        outcome = classify_exercise(_run(stdout=_GOOD_STDOUT, elapsed=HANDSHAKE_BUDGET_SECONDS + 0.5))

        assert outcome.ok is False
        assert outcome.cause is McpFailureCause.SLOW_STARTUP

    def test_a_prompt_clean_handshake_passes(self) -> None:
        outcome = classify_exercise(_run(stdout=_GOOD_STDOUT, elapsed=HANDSHAKE_BUDGET_SECONDS - 0.5))

        assert outcome.ok is True
        assert outcome.cause is None

    def test_a_stale_env_is_named_over_a_timeout(self) -> None:
        """A timeout is the least informative signal — a known signature in stderr wins."""
        run = _run(stderr=_IMPORT_ERROR_STDERR, elapsed=99.0, timed_out=True)

        assert classify_exercise(run).cause is McpFailureCause.STALE_TOOL_ENV


class TestMalformedOutputIsNotAHandshake:
    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "not json at all",
            json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}),
        ],
    )
    def test_it_fails(self, stdout: str) -> None:
        assert classify_exercise(_run(stdout=stdout)).ok is False


class TestTheSpawnSeam:
    def test_the_injected_spawn_receives_the_argv_and_its_run_is_classified(self) -> None:
        seen: list[list[str]] = []

        def spawn(argv: list[str]) -> ExerciseRun:
            seen.append(argv)
            return _run(stdout=_GOOD_STDOUT, elapsed=0.2)

        outcome = exercise_mcp_server(["t3", "mcp", "serve"], spawn=spawn)

        assert seen == [["t3", "mcp", "serve"]]
        assert outcome.ok is True
