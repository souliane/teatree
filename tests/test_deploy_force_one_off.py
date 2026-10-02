# test-path: cross-cutting — drives deploy/t3 as a real bash program (no src mirror).
"""Forced one-off dispatch keeps host handoffs and ignores an in-flight update."""

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
SYSTEM_PATH = os.defpath.strip(os.pathsep)

pytestmark = pytest.mark.skipif(
    shutil.which("bash", path=SYSTEM_PATH) is None,
    reason="needs a system bash (present in the deploy image and CI)",
)

_FLOCK = shutil.which("flock")


def _fork_checkout(root: Path) -> Path:
    entry = root / "vendor" / "teatree" / "deploy" / "t3"
    entry.parent.mkdir(parents=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    shutil.copy2(WRAPPER, entry.parent / "docker-compose.yml")
    (root / "pyproject.toml").write_text("[project]\nname = 'fork'\n", encoding="utf-8")
    return entry


def _docker_stub(*, home: Path, log: Path) -> str:
    browser_url = home / ".local" / "share" / "teatree" / "admin-browse-url"
    forward_plan = home / ".local" / "share" / "teatree" / "peer-forward-plan"
    return f"""#!/bin/bash
printf '%s\\n' "$*" >>{log}
if [ "$1" = inspect ]; then printf '%s' "bind $PWD"; exit 0; fi
if [ "$1" = version ]; then echo 99; exit 0; fi
for arg in "$@"; do
    if [ "$arg" = ps ]; then echo 'teatree-worker-1 teatree-worker False'; exit 0; fi
    if [ "$arg" = config ]; then exit 0; fi
    if [ "$arg" = run ]; then
        mkdir -p {browser_url.parent}
        if printf '%s\\n' "$*" | grep -q ' peer '; then
            printf 'action=up\\n' >{forward_plan}
        else
            printf 'http://127.0.0.1:8803/\\n' >{browser_url}
        fi
        exit 0
    fi
    if [ "$arg" = exec ]; then
        echo 'unexpected exec during forced one-off' >&2
        exit 99
    fi
done
exit 0
"""


@contextmanager
def _held_lock(path: Path) -> Iterator[None]:
    path.touch()
    assert _FLOCK is not None
    holder = subprocess.Popen([_FLOCK, str(path), "-c", "sleep 60"])
    try:
        yield
    finally:
        holder.kill()
        holder.wait()


def _run(
    tmp_path: Path, argv: list[str], *, update_lock: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    home = tmp_path / "home"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    browser_witness = tmp_path / "browser"
    forward_witness = tmp_path / "forward"

    docker = stub_dir / "docker"
    docker.write_text(_docker_stub(home=home, log=docker_log), encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for name, body in {
        "curl": "#!/bin/sh\nexit 0\n",
        "open": f"#!/bin/sh\nprintf '%s\\n' \"$*\" >{browser_witness}\n",
        "bash": f"#!/bin/sh\nprintf '%s\\n' \"$*\" >{forward_witness}\n",
    }.items():
        stub = stub_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    entry = _fork_checkout(tmp_path / "fork")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env.update(
        {
            "PATH": f"{stub_dir}{os.pathsep}{SYSTEM_PATH}",
            "TEATREE_HOST_HOME": str(home),
            "TEATREE_FORCE_ONE_OFF": "1",
            "TEATREE_UPDATE_WAIT_SECONDS": "0",
        }
    )
    if update_lock:
        env["TEATREE_DEPLOY_LOCK"] = str(tmp_path / "deploy.lock")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if update_lock:
        with _held_lock(Path(env["TEATREE_DEPLOY_LOCK"])):
            completed = subprocess.run(
                ["/bin/bash", str(entry), *argv], capture_output=True, text=True, check=False, env=env, cwd=elsewhere
            )
    else:
        completed = subprocess.run(
            ["/bin/bash", str(entry), *argv], capture_output=True, text=True, check=False, env=env, cwd=elsewhere
        )
    return completed, browser_witness, forward_witness


class TestForcedOneOffDispatch:
    def test_browser_handoff_runs_after_a_forced_one_off(self, tmp_path: Path) -> None:
        completed, browser, _ = _run(tmp_path, ["admin"])

        assert completed.returncode == 0, completed.stderr
        assert browser.read_text(encoding="utf-8").strip() == "http://127.0.0.1:8803/"

    def test_forward_handoff_runs_after_a_forced_one_off(self, tmp_path: Path) -> None:
        completed, _, forward = _run(tmp_path, ["peer", "up"])

        assert completed.returncode == 0, completed.stderr
        assert "peer-forward-plan" in forward.read_text(encoding="utf-8")

    def test_a_forced_one_off_handoff_names_its_venue_before_dispatching(self, tmp_path: Path) -> None:
        completed, _, _ = _run(tmp_path, ["admin"])

        assert completed.returncode == 0, completed.stderr
        assert "one-off dispatch was requested" in completed.stderr

    @pytest.mark.skipif(
        _FLOCK is None or not Path("/proc/locks").exists(),
        reason="needs flock + Linux /proc/locks (present in the deploy image and CI)",
    )
    def test_forced_one_off_bypasses_an_in_flight_update(self, tmp_path: Path) -> None:
        completed, _, _ = _run(tmp_path, ["doctor", "check"], update_lock=True)

        assert completed.returncode == 0, completed.stderr
        assert "update is in progress" not in completed.stderr
