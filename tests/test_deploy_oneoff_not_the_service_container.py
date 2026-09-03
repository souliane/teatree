"""An ephemeral `run --rm` one-off is not the worker `compose exec` targets.

Compose lists a one-off under the SAME service name as the long-lived service
container, so ``ps --status running`` answers with the one-off while ``exec``
resolves the service container. Measured on a paused stack: ``--status running``
returned the one-off, the wrapper read that as "the worker is up", and the exec
died on ``Container … is paused`` — leaving the one-off fallback, which would have
worked, unreachable for as long as any one-off existed.

The same answer feeds the source-mount guard, so it validated the ONE-OFF's mounts
and then dispatched into a container that may carry entirely different ones: the
guard passing on container A while the command runs in container B is exactly the
silent staleness it exists to prevent.

The wrapper is exercised for real — the genuine ``deploy/t3`` copied into a tmp
tree and run against a ``docker`` stub. Docker is the unstoppable external.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"

SYSTEM_PATH = os.defpath.strip(os.pathsep)

pytestmark = pytest.mark.skipif(
    shutil.which("bash", path=SYSTEM_PATH) is None,
    reason="needs a system bash (present on macOS, in the deploy image, and in CI)",
)

ONEOFF_ID = "teatree-worker-run-abc123"
WORKER_ID = "teatree-worker-1"

DISPATCHED_EXEC = "DISPATCHED-EXEC"
DISPATCHED_RUN = "DISPATCHED-RUN"
SERVICE_IMAGE = "teatree-worker:local"

DRIFT_MARKER = "runs a DIFFERENT source tree"

# Answers `compose ps`, both templated `docker inspect` reads, and the two dispatch
# hops. The container id is the last argument of an inspect; the label read and the
# mount read are told apart by the field each template names, never by the id.
DOCKER_STUB = f"""#!/usr/bin/env bash
last=""
for arg in "$@"; do last="$arg"; done

if [ "$1" = inspect ]; then
    case "$*" in
    *Mounts*)
        if [ "$last" = {ONEOFF_ID} ]; then
            printf 'bind %s' "${{STUB_ONEOFF_SOURCE:-}}"
        else
            printf 'bind %s' "${{STUB_WORKER_SOURCE:-}}"
        fi
        exit 0 ;;
    *Labels*)
        if [ "$last" = {ONEOFF_ID} ]; then printf 'True'; else printf 'False'; fi
        exit 0 ;;
    esac
    exit 0
fi

case "$1" in
version | image) exit 0 ;;
esac

for arg in "$@"; do
    case "$arg" in
    ps) printf '%s\\n' ${{STUB_RUNNING_IDS:-}}; exit 0 ;;
    config) printf '%s\\n' '{SERVICE_IMAGE}'; exit 0 ;;
    exec) printf '{DISPATCHED_EXEC}\\n'; exit 0 ;;
    run) printf '{DISPATCHED_RUN}\\n'; exit 0 ;;
    esac
done
exit 0
"""


def _fork_checkout(root: Path) -> Path:
    """The `<fork>/vendor/teatree/deploy/t3` layout that resolves TEATREE_SOURCE_MOUNT."""
    entry = root / "vendor" / "teatree" / "deploy" / "t3"
    entry.parent.mkdir(parents=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (root / "pyproject.toml").write_text("[project]\nname = 'fork'\n", encoding="utf-8")
    shutil.copy2(WRAPPER, entry.parent / "docker-compose.yml")
    return entry


def _run(tmp_path: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(DOCKER_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    entry = _fork_checkout(tmp_path / "fork")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{SYSTEM_PATH}"
    env["TEATREE_HOST_HOME"] = str(tmp_path / "home")
    # Short-circuits the host `glab` resolution — irrelevant here and not always present.
    env["GITLAB_TOKEN"] = "unused"
    env.update(env_extra)

    # Stand outside any checkout, so the invisible-checkout refusal has no tree to
    # fire on before the dispatch under test is reached.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)

    return subprocess.run(
        [str(entry), "doctor", "check"], capture_output=True, text=True, check=True, env=env, cwd=elsewhere
    )


class TestAOneOffIsNotARunningWorker:
    def test_a_one_off_alone_falls_through_to_the_one_off_fallback(self, tmp_path: Path) -> None:
        """A paused/absent worker must degrade to `run --rm`, not exec into a dead container."""
        proc = _run(tmp_path, STUB_RUNNING_IDS=ONEOFF_ID)

        assert DISPATCHED_RUN in proc.stdout
        assert DISPATCHED_EXEC not in proc.stdout

    def test_a_running_worker_still_execs(self, tmp_path: Path) -> None:
        proc = _run(tmp_path, STUB_RUNNING_IDS=WORKER_ID)

        assert DISPATCHED_EXEC in proc.stdout
        assert DISPATCHED_RUN not in proc.stdout

    def test_a_one_off_listed_first_does_not_displace_the_running_worker(self, tmp_path: Path) -> None:
        # Compose fixes no order between the two, so the answer must not depend on it.
        proc = _run(tmp_path, STUB_RUNNING_IDS=f"{ONEOFF_ID} {WORKER_ID}")

        assert DISPATCHED_EXEC in proc.stdout

    def test_nothing_running_still_falls_through(self, tmp_path: Path) -> None:
        proc = _run(tmp_path)

        assert DISPATCHED_RUN in proc.stdout


class TestTheMountGuardReadsTheContainerThatWillRun:
    def test_a_stale_worker_is_reported_even_when_a_one_off_mounts_this_checkout(self, tmp_path: Path) -> None:
        """The guard must not clear a drift on a container the command never runs in."""
        stale = "/Users/someone/workspace/of-autoclone-src"
        proc = _run(
            tmp_path,
            STUB_RUNNING_IDS=f"{ONEOFF_ID} {WORKER_ID}",
            STUB_ONEOFF_SOURCE=str(tmp_path / "fork"),
            STUB_WORKER_SOURCE=stale,
        )

        assert DRIFT_MARKER in proc.stderr
        assert stale in proc.stderr, "the tree the exec'd worker actually runs must be named"

    def test_a_current_worker_is_silent_even_when_a_one_off_is_stale(self, tmp_path: Path) -> None:
        """The mirror: a one-off's own mount is never evidence of drift."""
        proc = _run(
            tmp_path,
            STUB_RUNNING_IDS=f"{ONEOFF_ID} {WORKER_ID}",
            STUB_ONEOFF_SOURCE="/Users/someone/workspace/of-autoclone-src",
            STUB_WORKER_SOURCE=str(tmp_path / "fork"),
        )

        assert DRIFT_MARKER not in proc.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
