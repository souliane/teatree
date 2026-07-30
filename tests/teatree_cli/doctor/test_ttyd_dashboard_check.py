"""Tests for ``_check_ttyd_for_dashboard`` — flag missing ttyd on the admin box (#3263).

The dashboard "Debug session" button spawns a loopback ``ttyd`` terminal
(``teatree.agents.terminal_launcher.launch_ttyd``). ttyd is absent from the
Docker image's admin role by default, so the feature silently 500s. The check
flags a missing ttyd only when the box actually serves the dashboard
(``TEATREE_ROLE == "admin"``); it never gates the doctor exit code.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from teatree.cli.doctor.checks_runtime import _check_ttyd_for_dashboard


def _run(env: dict[str, str], *, ttyd_present: bool) -> tuple[bool, str]:
    out = io.StringIO()

    def _which(tool: str) -> str | None:
        return f"/usr/bin/{tool}" if (tool == "ttyd" and ttyd_present) else None

    with redirect_stdout(out), patch("shutil.which", side_effect=_which):
        ok = _check_ttyd_for_dashboard(env=env)
    return ok, out.getvalue()


class TestCheckTtydForDashboard:
    def test_warns_when_admin_role_and_ttyd_missing(self) -> None:
        ok, message = _run({"TEATREE_ROLE": "admin"}, ttyd_present=False)
        assert ok is True  # surfacing-only — never gates the doctor exit code
        assert "WARN" in message
        assert "ttyd" in message
        assert "Debug session" in message

    def test_silent_when_admin_role_and_ttyd_present(self) -> None:
        ok, message = _run({"TEATREE_ROLE": "admin"}, ttyd_present=True)
        assert ok is True
        assert "WARN" not in message
        assert "FAIL" not in message

    def test_silent_when_worker_role_and_ttyd_missing(self) -> None:
        ok, message = _run({"TEATREE_ROLE": "worker"}, ttyd_present=False)
        assert ok is True
        assert "WARN" not in message

    def test_silent_when_no_role_and_ttyd_missing(self) -> None:
        ok, message = _run({}, ttyd_present=False)
        assert ok is True
        assert "WARN" not in message

    def test_never_fails_the_doctor_exit_code(self) -> None:
        ok, _message = _run({"TEATREE_ROLE": "admin"}, ttyd_present=False)
        assert ok is True

    def test_reads_real_environ_when_env_is_none(self, monkeypatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        out = io.StringIO()
        with redirect_stdout(out), patch("shutil.which", return_value=None):
            ok = _check_ttyd_for_dashboard()
        assert ok is True
        assert "WARN" not in out.getvalue()
