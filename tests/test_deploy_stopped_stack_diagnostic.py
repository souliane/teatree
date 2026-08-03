"""``deploy/t3`` must name how to start the stack, never leave docker to explain (#3964).

Host ``t3`` is a shim, so a host with no deployment has nothing else to report
with. Docker's own text for the two ways that happens — a dead daemon, or an
image this host never built — names no teatree command, and on the never-built
path it reads as a failed registry pull for an image that only ever existed
locally.

A merely STOPPED stack is deliberately NOT an error: the one-off container is the
intended path there and works, so it must keep dispatching.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
WRAPPER = DEPLOY_DIR / "t3"

DISPATCHED_ONE_OFF = "DISPATCHED-RUN"
DISPATCHED_EXEC = "DISPATCHED-EXEC"
SERVICE_IMAGE = "teatree-worker:local"

# Reports which hop it was handed, and fails the daemon / image probes on demand
# so a test can put the host in either unavailable state.
DOCKER_STUB = f"""#!/usr/bin/env bash
case "$1" in
version) exit "${{STUB_DAEMON_EXIT:-0}}" ;;
image) exit "${{STUB_IMAGE_EXIT:-0}}" ;;
esac
for arg in "$@"; do
    case "$arg" in
    ps) printf '%s' "${{STUB_RUNNING_ID:-}}"; exit 0 ;;
    config) printf '%s\\n' "${{STUB_SERVICE_IMAGE:-}}"; exit 0 ;;
    run) printf '{DISPATCHED_ONE_OFF}\\n'; exit 0 ;;
    exec) printf '{DISPATCHED_EXEC}\\n'; exit 0 ;;
    esac
done
exit 0
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir(parents=True, exist_ok=True)
    return host_home


@pytest.fixture
def wrapper(tmp_path: Path) -> Path:
    """The real wrapper, copied out so the checkout it runs from is ours."""
    deploy = tmp_path / "checkout" / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    entry = deploy / "t3"
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


def _run(wrapper: Path, home: Path, tmp_path: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    stub_dir = home / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    docker = stub_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_SOURCE_MOUNT", "TEATREE_INVOCATION_CWD"}}
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["TEATREE_HOST_HOME"] = str(home)
    # Short-circuits the host `glab` resolution — irrelevant here and not always present.
    env["GITLAB_TOKEN"] = "unused"
    env["STUB_SERVICE_IMAGE"] = SERVICE_IMAGE
    env.update(env_extra)

    # A plain directory, so the invisible-checkout refusal has no tree to fire on.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)

    return subprocess.run(
        [str(wrapper), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=elsewhere,
        check=False,
    )


class TestAnUnavailableStackIsNamed:
    def test_an_unreachable_daemon_names_the_deploy_script(self, wrapper: Path, home: Path, tmp_path: Path) -> None:
        proc = _run(wrapper, home, tmp_path, STUB_DAEMON_EXIT="1")

        assert proc.returncode != 0
        assert DISPATCHED_ONE_OFF not in proc.stdout
        assert "deploy/deploy.sh" in proc.stderr
        assert "Docker daemon" in proc.stderr

    def test_a_never_built_image_names_the_deploy_script_and_the_image(
        self, wrapper: Path, home: Path, tmp_path: Path
    ) -> None:
        proc = _run(wrapper, home, tmp_path, STUB_IMAGE_EXIT="1")

        assert proc.returncode != 0
        assert DISPATCHED_ONE_OFF not in proc.stdout
        assert "deploy/deploy.sh" in proc.stderr
        assert SERVICE_IMAGE in proc.stderr

    def test_the_named_deploy_script_is_the_wrappers_own_checkout(
        self, wrapper: Path, home: Path, tmp_path: Path
    ) -> None:
        """A vendored fork must be told about ITS deploy.sh, not a path from elsewhere."""
        proc = _run(wrapper, home, tmp_path, STUB_DAEMON_EXIT="1")

        assert str(tmp_path / "checkout" / "deploy" / "deploy.sh") in proc.stderr

    def test_an_unresolvable_service_image_is_left_to_the_one_off_to_report(
        self, wrapper: Path, home: Path, tmp_path: Path
    ) -> None:
        """Compose answering nothing is not evidence of an absent image."""
        proc = _run(wrapper, home, tmp_path, STUB_SERVICE_IMAGE="", STUB_IMAGE_EXIT="1")

        assert proc.returncode == 0
        assert DISPATCHED_ONE_OFF in proc.stdout


class TestAStoppedStackStillDispatches:
    """Behaviour preservation: only an UNAVAILABLE stack is an error."""

    def test_a_stopped_stack_with_a_built_image_runs_the_one_off(
        self, wrapper: Path, home: Path, tmp_path: Path
    ) -> None:
        proc = _run(wrapper, home, tmp_path)

        assert proc.returncode == 0
        assert DISPATCHED_ONE_OFF in proc.stdout

    def test_a_running_worker_execs_without_preflighting(self, wrapper: Path, home: Path, tmp_path: Path) -> None:
        """The `ps` answer already proves the daemon and the image, so neither is re-probed."""
        proc = _run(wrapper, home, tmp_path, STUB_RUNNING_ID="abc123", STUB_DAEMON_EXIT="1", STUB_IMAGE_EXIT="1")

        assert proc.returncode == 0
        assert DISPATCHED_EXEC in proc.stdout
