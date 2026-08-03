"""``deploy/t3`` must run with no Python of any kind on the host (#3964).

The whole point of the shim is that a host needs no interpreter, no uv, and no
teatree install to invoke ``t3`` — the CLI lives in the image. A wrapper that
quietly grew a ``python``/``uv``/``t3`` dependency would keep working on the
maintainer's box (where all three exist) and fail only on the hosts the shim was
written for, so the absence is pinned rather than assumed.

PATH here carries the shell essentials and the ``docker`` stub, and nothing else.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._deploy_wrapper_paths import container_worktree_root

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
WRAPPER = DEPLOY_DIR / "t3"

CONTAINER_WORKTREE_ROOT = container_worktree_root()
DISPATCHED = "DISPATCHED"

# Every external the wrapper legitimately reaches for. Deliberately excludes
# python/python3/uv/t3 — their absence is the subject.
_SHELL_ESSENTIALS = (
    "bash",
    "sh",
    "env",
    "uname",
    "stat",
    "install",
    "dirname",
    "basename",
    "head",
    "awk",
    "readlink",
    "mkdir",
    "cat",
)

DOCKER_STUB = f"""#!/usr/bin/env bash
case "$1" in
version | image) exit 0 ;;
esac
for arg in "$@"; do
    case "$arg" in
    ps) exit 0 ;;
    config) printf 'teatree-worker:local\\n'; exit 0 ;;
    run | exec) printf '{DISPATCHED} %s\\n' "${{TEATREE_INVOCATION_CWD-unset}}"; exit 0 ;;
    esac
done
exit 0
"""


@pytest.fixture
def bare_bin(tmp_path: Path) -> Path:
    """A PATH directory with the shell essentials and nothing python-shaped."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in _SHELL_ESSENTIALS:
        source = shutil.which(tool)
        if source:
            (fake_bin / tool).symlink_to(source)
    docker = fake_bin / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)
    return fake_bin


@pytest.fixture
def wrapper(tmp_path: Path) -> Path:
    deploy = tmp_path / "checkout" / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    entry = deploy / "t3"
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


class TestTheHostNeedsNoPythonToolchain:
    def test_a_bind_mounted_worktree_dispatches_on_a_python_free_path(
        self, tmp_path: Path, bare_bin: Path, wrapper: Path
    ) -> None:
        home = tmp_path / "home"
        worktree = home / "workspace" / "t3-workspaces" / "4242-ticket" / "teatree"
        (worktree / ".git").mkdir(parents=True)

        proc = subprocess.run(
            [str(wrapper), "--help"],
            capture_output=True,
            text=True,
            env={"PATH": str(bare_bin), "HOME": str(home), "TEATREE_HOST_HOME": str(home)},
            cwd=worktree,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert f"{DISPATCHED} {CONTAINER_WORKTREE_ROOT}/4242-ticket/teatree" in proc.stdout

    def test_no_interpreter_is_reachable_from_that_path(self, bare_bin: Path) -> None:
        """Guards the guard: a PATH that still leaks a python proves nothing."""
        env_path = str(bare_bin)

        for absent in ("python", "python3", "uv", "t3"):
            assert shutil.which(absent, path=env_path) is None

        assert shutil.which("bash", path=env_path) is not None
        assert os.sep in env_path
