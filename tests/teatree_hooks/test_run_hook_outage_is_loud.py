"""A total hook outage must announce itself, not look like "nothing to do".

``run-hook.sh`` picks the newest available Python >= 3.11 and fails OPEN when it
finds none — the crash-proof contract. But the fail-open was a bare ``exit 0``:
every gate, the skill suggester, the engagement seam and the loop bootstrap were
dead, and the session was byte-identical to one where the hooks simply had
nothing to say. That silence is the whole bug class behind "teatree behaves as
if the owner never opted in".

The shim now says so on stderr while still exiting 0. ``TaskCreated`` is the one
event that stays silent: the harness aborts task creation on ANY stderr from its
hooks (``hooks/CLAUDE.md``), so warning there would turn a degraded install into
a hard lockout.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUN_HOOK = _REPO_ROOT / "hooks" / "scripts" / "run-hook.sh"


@pytest.fixture
def no_python_path(tmp_path: Path) -> str:
    """A PATH with the shell essentials but no ``python*`` interpreter at all."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("bash", "sh", "env", "command"):
        source = shutil.which(tool)
        if source:
            (fake_bin / tool).symlink_to(source)
    return str(fake_bin)


def _run(args: list[str], path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("bash") or "bash", str(_RUN_HOOK), *args],
        capture_output=True,
        text=True,
        env={"PATH": path},
        check=False,
        timeout=30,
    )


class TestNoUsableInterpreterIsLoud:
    def test_outage_is_announced_on_stderr(self, no_python_path: str) -> None:
        proc = _run(["/nonexistent/hook.py", "--event", "UserPromptSubmit"], no_python_path)

        assert proc.stderr.strip(), "a total hook outage must not be silent"
        # Names the condition and the remedy, not just a generic error.
        assert "3.11" in proc.stderr
        assert "hook" in proc.stderr.lower()

    def test_outage_still_fails_open(self, no_python_path: str) -> None:
        # Loud, but never session-breaking — the crash-proof contract holds.
        assert _run(["/nonexistent/hook.py", "--event", "UserPromptSubmit"], no_python_path).returncode == 0

    def test_task_created_stays_silent(self, no_python_path: str) -> None:
        # The harness aborts task creation on ANY stderr from a TaskCreated
        # hook, so the warning must not turn a degraded install into a lockout.
        proc = _run(["/nonexistent/hook.py", "--event", "TaskCreated"], no_python_path)

        assert proc.stderr == ""
        assert proc.returncode == 0


class TestUsableInterpreterStaysSilent:
    def test_happy_path_emits_no_warning(self, tmp_path: Path) -> None:
        script = tmp_path / "ok.py"
        script.write_text("print('ran')\n", encoding="utf-8")

        import os  # noqa: PLC0415 — local: only this test needs the real PATH

        proc = _run([str(script)], os.environ.get("PATH", ""))

        assert proc.returncode == 0
        assert proc.stdout.strip() == "ran"
        assert proc.stderr == ""
