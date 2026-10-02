"""A verification lane must announce its FAILURE, not only its success.

A shell pipeline reports its LAST stage's status, so ``bash dev/<lane>.sh 2>&1 |
tail -25`` exits with ``tail``'s 0 no matter how the lane ended. ``set -o
pipefail`` inside the lane cannot reach that pipe — it governs only pipelines the
script itself builds, and the pipe here belongs to the CALLER. Nothing a script
does can restore its own exit code once a caller has piped it away.

So the verdict has to live in the OUTPUT rather than only in ``$?``. These lanes
announced only their success: a green run ended with an unmistakable banner, and
a red run ended with whatever its failing step happened to print last. Piped into
``tail``, the red run therefore showed no verdict at all while the pipeline
reported 0 — a lane that reads clean while failing, which is worse than no lane
because it is trusted.

The guard is an EXIT trap emitting a FAILED banner, so the final line of a piped
run is always a verdict, in both directions. ``tests/test_ci_shuffle_lane_scope.py``
pins the neighbouring half of the same hazard: that a lane never pipes pytest
*internally*, where ``pipefail`` does apply.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_BASH = shutil.which("bash") or "/bin/bash"
_REPO_ROOT = Path(__file__).resolve().parents[1]

# The lanes that announce a verdict banner on success. Each is a gate whose green
# result is trusted, and each dies mid-step on failure — so each needs the
# failure banner for its piped output to be readable.
_VERDICT_LANES = (
    "dev/ci-parity-fast.sh",
    "dev/ci-parity.sh",
    "dev/test-shuffle.sh",
)


@pytest.fixture
def failing_uv_path(tmp_path: Path) -> str:
    """A ``PATH`` whose ``uv`` fails immediately, so a lane dies in its FIRST step.

    Every lane below reaches ``uv`` within its first step, so this drives a real
    red run of the real script in well under a second — no stubbed copy of the
    lane, which would pin a duplicate rather than the script that actually runs.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text("#!/bin/sh\necho 'simulated lane failure' >&2\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _run(command: str, path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, "-c", command],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": path},
        cwd=_REPO_ROOT,
        check=False,
    )


@pytest.mark.parametrize("lane", _VERDICT_LANES)
class TestPipedLaneCannotReadAsClean:
    def test_failure_banner_survives_a_pipe_into_tail(self, lane: str, failing_uv_path: str) -> None:
        """The reported shape: the lane is red, but the pipeline's status is ``tail``'s."""
        script = shlex.quote(str(_REPO_ROOT / lane))
        result = _run(f"bash {script} 2>&1 | tail -5", failing_uv_path)

        # The premise, asserted rather than assumed: the caller's pipe really does
        # erase the failure from the exit status. If this ever stops holding, the
        # banner below is belt-and-braces rather than the only signal.
        assert result.returncode == 0, (
            "expected the caller's pipeline to mask the lane's non-zero exit "
            f"(that is the whole hazard), got {result.returncode}"
        )
        assert "FAILED" in result.stdout, (
            f"{lane} produced no failure verdict in the last lines of a red run, so piped into "
            "`tail` it is indistinguishable from a clean one — while the pipeline reports success."
        )

    def test_unpiped_run_still_exits_non_zero(self, lane: str, failing_uv_path: str) -> None:
        """The banner must REPORT the failure, never absorb it.

        A trap that ends in a bare ``exit`` (or forgets to re-raise the code) would
        turn every red lane green for callers that do not pipe — trading one masked
        exit status for a worse one.
        """
        script = shlex.quote(str(_REPO_ROOT / lane))
        result = _run(f"bash {script} >/dev/null 2>&1", failing_uv_path)

        assert result.returncode != 0, f"{lane} swallowed its own failing exit code"
