# test-path: cross-cutting
"""deploy.sh's exit code must never be swallowed behind an internal pipe (#4822).

A piped invocation returning the pipe's own status rather than the underlying
command's is a known failure class — a run that printed a FATAL diagnostic but
still exited 0 produced two consecutive false success reports on the box
before the log was read (same class as `t3 push` exiting 0 on a worktree).

deploy.sh already carries `set -euo pipefail` as its first statement, and
every FATAL branch already ends the run with an explicit `exit 1` rather than
falling off the end of a pipeline — see `tests/test_deploy_staged_swap.py`'s
many `returncode != 0` assertions for the end-to-end proof of the second half.
This module pins the code invariant that keeps both true: pipefail active from
the very first line, and never turned back off, so an internal pipe (e.g.
`worker_route_answers`'s `compose exec ... | grep -q ...`) can only ever
report ITS real failure, never silently succeed on a failed left-hand side.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "deploy.sh"


class TestPipefailIsSetAndNeverUnset:
    def test_set_euo_pipefail_is_the_first_executable_line(self) -> None:
        lines = [
            line
            for line in _SCRIPT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert lines[0] == "set -euo pipefail", (
            "an internal pipe can only propagate its real failure to deploy.sh's own "
            "exit code with pipefail active from the first statement onward"
        )

    def test_pipefail_is_never_turned_back_off(self) -> None:
        code = "\n".join(
            line for line in _SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        assert "set +o pipefail" not in code
        assert "set +e" not in code

    def test_every_fatal_branch_ends_the_run_explicitly(self) -> None:
        # A FATAL diagnostic with no reachable `exit`/`return` would fall through
        # to whatever runs next and could still report success — every FATAL
        # line in this script is paired with one within the same statement.
        lines = _SCRIPT.read_text(encoding="utf-8").splitlines()
        fatal_lines = [i for i, line in enumerate(lines) if "FATAL" in line and line.lstrip().startswith("echo")]
        assert fatal_lines, "no FATAL diagnostics found — this test's own anchor is stale"
        for i in fatal_lines:
            window = "\n".join(lines[i : i + 4])
            assert "exit 1" in window or "return 1" in window, (
                f"line {i + 1} prints FATAL with no exit/return within the next few lines: {lines[i]!r}"
            )
