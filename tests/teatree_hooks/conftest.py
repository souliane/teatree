"""Shared fixtures for the hook-gate tests."""

import io
import os
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.hooks import banned_terms_cli, banned_terms_scanner
from teatree.utils.run import CommandFailedError


def _scan_in_process(
    cmd: Sequence[str],
    *,
    expected_codes: Sequence[int] | None = (0,),
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    **_unused: object,
) -> CompletedProcess[str]:
    """``run_allowed_to_fail`` for ``check-banned-terms.sh``, minus the process."""
    del timeout  # nothing to wait for: the matcher runs here
    out, err = io.StringIO(), io.StringIO()
    with (
        patch.dict(os.environ, env or {}, clear=env is not None),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        returncode = banned_terms_cli.main(list(cmd[1:]))
    result = CompletedProcess(list(cmd), returncode, out.getvalue(), err.getvalue())
    if expected_codes is not None and returncode not in expected_codes:
        raise CommandFailedError(cmd, returncode, result.stdout, result.stderr)
    return result


@pytest.fixture
def in_process_banned_terms_scanner(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the banned-terms matcher in-process rather than exec'ing the shell hook (#4781).

    ``check-banned-terms.sh`` execs ``uv run ... python -m teatree.hooks.banned_terms_cli``,
    so every ``scan_text`` pays a cold interpreter start (measured 0.33/0.69/0.95s idle)
    for a regex over one commit message. Under CI contention that startup alone blows
    ``SCAN_TIMEOUT_DEFAULT_S`` and the gate reports a timeout marker where the test
    asserted a policy verdict. The verdict here still comes from ``banned_terms_cli`` —
    the very module the script execs — so only the process boundary is removed, never
    the policy, the matcher, the term source or the exit-code contract.

    A test marked ``real_banned_terms_scanner`` keeps the real subprocess, which is how
    the shell script's own contract stays covered. The broader ``integration`` marker is
    NOT that signal: classes here carry it for the real ``git`` they run, and reading it
    as an opt-out would leave most of the module exec'ing the scanner anyway.
    """
    if request.node.get_closest_marker("real_banned_terms_scanner"):
        return
    monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _scan_in_process)
