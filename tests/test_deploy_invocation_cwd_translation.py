"""``deploy/t3`` must translate the operator's cwd for EVERY bind-mounted tree.

``docker compose exec`` starts the CLI in the image's own WORKDIR, so the host
cwd never crosses the boundary on its own: inside the container ``PWD`` reports
``/home/teatree`` no matter where ``t3`` was typed. The wrapper is the only layer
that knows the mount mapping, so it translates the cwd into container coordinates
and exports it as ``TEATREE_INVOCATION_CWD`` for the CLI to read.

Translating only the SOURCE mount left out the two roots worktrees actually live
in, so every worktree-scoped command resolved from the image WORKDIR and failed
with ``Cannot auto-detect worktree from /home/teatree``. This file pins that each
mounted root translates, that the deepest matching mount wins, and that a cwd
outside every mount stays untranslated (the CLI must keep its own cwd rather than
be handed a path that means something else on the other side).

The wrapper is exercised for real, with a ``docker`` stub reporting what it was
handed — docker being the one unstoppable external.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
WRAPPER = DEPLOY_DIR / "t3"

CONTAINER_SOURCE_DIR = "/home/teatree/teatree"
CONTAINER_WORKTREE_ROOT = "/home/teatree/workspace/t3-workspaces"
CONTAINER_AUTO_ISOLATED_ROOT = "/home/teatree/.local/share/teatree-worktrees"

UNSET = "<unset>"

DOCKER_STUB = f"""#!/usr/bin/env bash
for arg in "$@"; do
    [ "$arg" = ps ] && exit 0
done
printf 'TEATREE_INVOCATION_CWD=%s\\n' "${{TEATREE_INVOCATION_CWD-{UNSET}}}"
"""


def _build_fork(root: Path) -> Path:
    """A vendored fork carrying the real wrapper — the layout an operator runs."""
    fork = root / "fork"
    deploy = fork / "vendor" / "teatree" / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    entry = deploy / "t3"
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (fork / "pyproject.toml").write_text('[project]\nname = "fork"\n', encoding="utf-8")
    return fork


def _invoke_from(fork: Path, home: Path, cwd: Path) -> str:
    """Run the wrapper standing in *cwd*; return the invocation cwd it exported."""
    stub_dir = home / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    docker = stub_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_SOURCE_MOUNT", "TEATREE_INVOCATION_CWD"}}
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["TEATREE_HOST_HOME"] = str(home)

    proc = subprocess.run(
        [str(fork / "vendor" / "teatree" / "deploy" / "t3"), "--help"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=cwd,
    )
    wiring = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return wiring["TEATREE_INVOCATION_CWD"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


class TestWorktreeRootsTranslate:
    """The regression: a cwd inside a worktree used to fall through untranslated."""

    def test_a_ticket_worktree_translates_to_its_container_path(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        worktree = home / "workspace" / "t3-workspaces" / "1234-ticket" / "backend"
        worktree.mkdir(parents=True)

        assert _invoke_from(fork, home, worktree) == f"{CONTAINER_WORKTREE_ROOT}/1234-ticket/backend"

    def test_the_worktree_root_itself_translates(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        root = home / "workspace" / "t3-workspaces"
        root.mkdir(parents=True)

        assert _invoke_from(fork, home, root) == CONTAINER_WORKTREE_ROOT

    def test_the_auto_isolated_worktree_root_translates(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        env_root = home / ".local" / "share" / "teatree-worktrees" / "wt42"
        env_root.mkdir(parents=True)

        assert _invoke_from(fork, home, env_root) == f"{CONTAINER_AUTO_ISOLATED_ROOT}/wt42"


class TestSourceMountStillTranslates:
    def test_the_fork_root_translates_to_the_container_source_dir(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)

        assert _invoke_from(fork, home, fork) == CONTAINER_SOURCE_DIR

    def test_a_subdir_of_the_source_mount_keeps_its_suffix(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        subdir = fork / "src" / "teatree"
        subdir.mkdir(parents=True)

        assert _invoke_from(fork, home, subdir) == f"{CONTAINER_SOURCE_DIR}/src/teatree"


class TestSymlinkedHostHome:
    def test_a_logical_host_home_still_matches_the_physical_cwd(self, tmp_path: Path) -> None:
        """``pwd -P`` resolves the cwd, so the prefix must be resolved too.

        A host whose home traverses a symlink (``/home`` -> ``/usr/home``, an
        NFS-mounted home, macOS's ``/tmp`` -> ``/private/tmp``) would otherwise
        never match, and the cwd would fall through untranslated on exactly
        those hosts.
        """
        fork = _build_fork(tmp_path)
        real_home = tmp_path / "real-home"
        worktree = real_home / "workspace" / "t3-workspaces" / "1234-ticket"
        worktree.mkdir(parents=True)
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home)

        translated = _invoke_from(fork, linked_home, worktree)

        assert translated == f"{CONTAINER_WORKTREE_ROOT}/1234-ticket"


class TestUnmappableCwd:
    def test_a_cwd_outside_every_mount_stays_untranslated(self, tmp_path: Path, home: Path) -> None:
        """Handing over an untranslatable path would name something else inside."""
        fork = _build_fork(tmp_path)
        outside = tmp_path / "elsewhere"
        outside.mkdir(parents=True)
        home.mkdir(parents=True, exist_ok=True)

        assert _invoke_from(fork, home, outside) == UNSET

    def test_an_explicit_invocation_cwd_is_never_overwritten(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        worktree = home / "workspace" / "t3-workspaces" / "1234-ticket"
        worktree.mkdir(parents=True)

        stub_dir = home / "stub-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        docker = stub_dir / "docker"
        docker.write_text(DOCKER_STUB, encoding="utf-8")
        docker.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "TEATREE_SOURCE_MOUNT"}
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["TEATREE_HOST_HOME"] = str(home)
        env["TEATREE_INVOCATION_CWD"] = "/pinned/by/operator"

        proc = subprocess.run(
            [str(fork / "vendor" / "teatree" / "deploy" / "t3"), "--help"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
            cwd=worktree,
        )

        assert "TEATREE_INVOCATION_CWD=/pinned/by/operator" in proc.stdout
