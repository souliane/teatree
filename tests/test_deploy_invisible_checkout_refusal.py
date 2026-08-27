"""``deploy/t3`` must refuse to dispatch from a checkout the container cannot see.

The container carries its OWN copy of the teatree source — a Docker *volume*, not
the host tree — while the workspace and env roots are bind mounts at identical
paths. So an untranslatable cwd is not a harmless degradation when the operator is
standing in a repository: ``docker compose exec`` starts the CLI in the image
WORKDIR and every cwd-sensitive command then resolves against the container's copy,
silently operating on the wrong tree while reporting success.

The refusal is scoped to a cwd inside a *checkout* on purpose. A cwd with no
enclosing checkout (``~``, ``/tmp``) has no tree to be wrong about, and every
cwd-insensitive command run from there must keep working exactly as before —
refusing those would break ``t3 doctor`` / ``t3 info`` from an operator's home
directory to guard a hazard that cannot arise there.

The wrapper is exercised for real, with a ``docker`` stub reporting what it was
handed — docker being the one unstoppable external.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._deploy_wrapper_paths import container_source_dir, container_worktree_root

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
WRAPPER = DEPLOY_DIR / "t3"

CONTAINER_SOURCE_DIR = container_source_dir()
CONTAINER_WORKTREE_ROOT = container_worktree_root()

DISPATCHED = "DISPATCHED"
UNSET = "<unset>"

DOCKER_STUB = f"""#!/usr/bin/env bash
for arg in "$@"; do
    [ "$arg" = ps ] && exit 0
done
printf '{DISPATCHED} TEATREE_INVOCATION_CWD=%s\\n' "${{TEATREE_INVOCATION_CWD-{UNSET}}}"
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


def _run(fork: Path, home: Path, cwd: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    stub_dir = home / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    docker = stub_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_SOURCE_MOUNT", "TEATREE_INVOCATION_CWD"}}
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["TEATREE_HOST_HOME"] = str(home)
    env.update(env_extra)

    return subprocess.run(
        [str(fork / "vendor" / "teatree" / "deploy" / "t3"), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


class TestHostOnlyCheckoutIsRefused:
    def test_a_checkout_outside_every_mount_never_reaches_docker(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        checkout = tmp_path / "host-only-clone"
        (checkout / ".git").mkdir(parents=True)

        proc = _run(fork, home, checkout)

        assert proc.returncode != 0
        assert DISPATCHED not in proc.stdout
        assert str(checkout) in proc.stderr

    def test_the_refusal_names_the_roots_a_working_directory_can_sit_under(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        checkout = tmp_path / "host-only-clone"
        (checkout / ".git").mkdir(parents=True)

        proc = _run(fork, home, checkout)

        assert str(home / "workspace" / "t3-workspaces") in proc.stderr
        assert "TEATREE_INVOCATION_CWD" in proc.stderr

    def test_the_refusal_names_no_root_a_cwd_cannot_be_translated_from(self, tmp_path: Path, home: Path) -> None:
        """Mounted is not usable-as-a-cwd: an advertised root that still refuses misleads.

        The credential and session planes are bind mounts, so a file under them is
        readable inside — but the wrapper translates only the workspace, worktree and
        source roots, so a checkout under the others is refused exactly like this one.
        """
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        checkout = tmp_path / "host-only-clone"
        (checkout / ".git").mkdir(parents=True)

        proc = _run(fork, home, checkout)

        # Matched as a whole LISTED line: `~/.local/share/teatree` is a prefix of the
        # worktree root the refusal legitimately names, so a substring test cannot tell
        # the two apart.
        listed = {line.strip() for line in proc.stderr.splitlines()}
        for untranslatable in (".claude/projects", ".password-store", ".gnupg", ".local/share/teatree"):
            assert str(home / untranslatable) not in listed

    def test_a_subdirectory_of_the_checkout_is_refused_too(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        checkout = tmp_path / "host-only-clone"
        (checkout / ".git").mkdir(parents=True)
        nested = checkout / "src" / "teatree"
        nested.mkdir(parents=True)

        proc = _run(fork, home, nested)

        assert proc.returncode != 0
        assert DISPATCHED not in proc.stdout

    def test_a_linked_worktree_whose_dot_git_is_a_file_is_a_checkout(self, tmp_path: Path, home: Path) -> None:
        """Teatree's own worktrees carry a ``.git`` FILE, not a directory."""
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        linked = tmp_path / "host-only-worktree"
        linked.mkdir(parents=True)
        (linked / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")

        proc = _run(fork, home, linked)

        assert proc.returncode != 0
        assert DISPATCHED not in proc.stdout


class TestVisibleCheckoutsStillDispatch:
    def test_a_checkout_under_the_worktree_root_dispatches_translated(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        worktree = home / "workspace" / "t3-workspaces" / "1234-ticket" / "teatree"
        (worktree / ".git").mkdir(parents=True)

        proc = _run(fork, home, worktree)

        assert proc.returncode == 0
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={CONTAINER_WORKTREE_ROOT}/1234-ticket/teatree" in proc.stdout

    def test_the_mounted_source_checkout_dispatches_translated(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        (fork / ".git").mkdir(parents=True)

        proc = _run(fork, home, fork)

        assert proc.returncode == 0
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={CONTAINER_SOURCE_DIR}" in proc.stdout


class TestNonCheckoutCwdIsUnchanged:
    """Behaviour preservation: only a *checkout* can be the wrong tree."""

    def test_a_plain_directory_outside_every_mount_still_dispatches(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(parents=True)

        proc = _run(fork, home, elsewhere)

        assert proc.returncode == 0
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={UNSET}" in proc.stdout

    def test_an_explicit_invocation_cwd_defeats_the_refusal(self, tmp_path: Path, home: Path) -> None:
        """The operator named the container-side tree, so nothing is being guessed."""
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        checkout = tmp_path / "host-only-clone"
        (checkout / ".git").mkdir(parents=True)

        proc = _run(fork, home, checkout, TEATREE_INVOCATION_CWD=CONTAINER_SOURCE_DIR)

        assert proc.returncode == 0
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={CONTAINER_SOURCE_DIR}" in proc.stdout
