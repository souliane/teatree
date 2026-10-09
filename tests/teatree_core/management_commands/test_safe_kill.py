"""Tests for the ``t3 teatree safe-kill`` management command (#2225).

The command is the runnable surface the PreToolUse raw-pid-kill deny points the
agent at. These tests pin its wiring: it forwards pid + hang_cause to
:func:`teatree.core.safe_kill.safe_kill`, prints a confirmation when the signal
was sent, and prints the refusal evidence + exits non-zero when the guard
refuses. The guard logic itself is covered by ``test_safe_kill_guard.py``; here
the core call is patched so only the command wiring is under test.
"""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.safe_kill import Liveness, SafeKillVerdict, TargetIdentity

_ALLOWED = SafeKillVerdict(
    allowed=True,
    reason="",
    identity=TargetIdentity(session_id="sess-dead", task_id=7, is_dead_target=True),
    liveness=Liveness(stat="Z", cpu_sample_1=0.0, cpu_sample_2=0.0, output_advanced=False),
)
_REFUSED = SafeKillVerdict(
    allowed=False,
    reason="REFUSED to signal pid 4242: process is in STAT R+ ... confirm the target id with the user",
    identity=TargetIdentity(session_id="sess-live", task_id=9, is_dead_target=True),
    liveness=Liveness(stat="R+", cpu_sample_1=9.0, cpu_sample_2=8.0, output_advanced=True),
)


def _call(*args: str) -> str:
    buf = StringIO()
    call_command("safe_kill", *args, stdout=buf)
    return buf.getvalue()


class TestSafeKillCommand:
    def test_forwards_pid_and_hang_cause_to_the_guard(self) -> None:
        with patch("teatree.core.management.commands.safe_kill.safe_kill", return_value=_ALLOWED) as mocked:
            out = _call("4242", "--hang-cause", "agent crashed, no output since")
        mocked.assert_called_once_with(4242, hang_cause="agent crashed, no output since")
        assert "signalled pid 4242" in out
        assert "sess-dead" in out

    def test_refusal_exits_nonzero_and_prints_evidence(self) -> None:
        with (
            patch("teatree.core.management.commands.safe_kill.safe_kill", return_value=_REFUSED),
            pytest.raises(SystemExit) as exc,
        ):
            _call("4242", "--hang-cause", "looks idle")
        assert "REFUSED to signal pid 4242" in str(exc.value)

    def test_missing_hang_cause_defaults_to_empty_and_is_refused_by_the_guard(self) -> None:
        with (
            patch("teatree.core.management.commands.safe_kill.safe_kill", return_value=_REFUSED) as mocked,
            pytest.raises(SystemExit),
        ):
            _call("4242")
        mocked.assert_called_once_with(4242, hang_cause="")
