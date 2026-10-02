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
import re
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

        Some state roots are readable inside the container, but the wrapper translates
        only workspace, worktree, and source roots. Agent homes are private volumes and
        credential/data binds are not valid checkout roots, so none is advertised.
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


class TestTheHostIdentityWorkspaceMountIsTranslatable:
    """The wrapper's visibility model must cover the identity mount the wrapper itself adds.

    ``deploy/t3`` appends ``-f docker-compose.host-identity.yml`` whenever the host home
    differs from the container's, and that file binds ``${TEATREE_HOST_HOME}/workspace``
    at PATH IDENTITY into ``teatree-worker`` and ``teatree-admin``. So on any such host
    every checkout under the workspace root is readable inside the container under the
    SAME absolute path — measured on a Docker Desktop box as
    ``/host_mnt/<home>/workspace -> <home>/workspace``.

    ``MOUNT_PAIRS`` listed only the ``t3-workspaces`` subtree, the worktree root and the
    source mount, so a sibling checkout under the workspace root was refused as "not
    visible inside the container" while being visible at its own path. The refusal named
    two remedies and neither was usable, which is what drove agents to a host ``git
    push`` and produced human-authored MRs the owner can never approve.
    """

    def test_a_sibling_checkout_under_the_workspace_root_dispatches_at_path_identity(
        self, tmp_path: Path, home: Path
    ) -> None:
        fork = _build_fork(tmp_path)
        checkout = home / "workspace" / "some-org" / "some-repo"
        (checkout / ".git").mkdir(parents=True)

        proc = _run(fork, home, checkout)

        assert proc.returncode == 0, proc.stderr
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={checkout}" in proc.stdout

    def test_a_nested_directory_of_such_a_checkout_translates_too(self, tmp_path: Path, home: Path) -> None:
        fork = _build_fork(tmp_path)
        nested = home / "workspace" / "some-org" / "some-repo" / "src" / "pkg"
        (home / "workspace" / "some-org" / "some-repo" / ".git").mkdir(parents=True)
        nested.mkdir(parents=True)

        proc = _run(fork, home, nested)

        assert proc.returncode == 0, proc.stderr
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={nested}" in proc.stdout

    def test_the_more_specific_worktree_root_still_wins_over_the_identity_root(
        self, tmp_path: Path, home: Path
    ) -> None:
        """``t3-workspaces`` nests INSIDE the workspace root and has its own canonical target.

        Every ``Worktree`` row records the canonical ``/home/teatree/workspace/t3-workspaces``
        path, so the identity pair must never shadow it — it is appended last for that reason.
        """
        fork = _build_fork(tmp_path)
        worktree = home / "workspace" / "t3-workspaces" / "1234-ticket" / "teatree"
        (worktree / ".git").mkdir(parents=True)

        proc = _run(fork, home, worktree)

        assert proc.returncode == 0, proc.stderr
        assert f"{DISPATCHED} TEATREE_INVOCATION_CWD={CONTAINER_WORKTREE_ROOT}/1234-ticket/teatree" in proc.stdout

    def test_the_identity_pair_and_the_identity_compose_file_move_together(self) -> None:
        """The translation may only claim the root the wrapper actually bound.

        On the box the two homes coincide, the overlay file is NOT added, and the base
        compose binds only the ``t3-workspaces`` subtree — so translating the whole
        workspace root there would claim a mount the container does not have, and
        ``/home/teatree/workspace`` is the clones VOLUME, a different tree from the
        host's. That branch cannot be exercised behaviourally off-box (a test cannot
        create ``/home/teatree``), so the invariant is asserted where it lives: ONE
        flag, set in the same conditional that appends the overlay file, is the only
        thing that admits the pair.
        """
        wrapper = WRAPPER.read_text(encoding="utf-8")

        guard = re.search(
            r'if \[ "\$TEATREE_HOST_HOME" != "\$CONTAINER_HOME" \]; then\n(?P<body>(?:[ \t]+\S[^\n]*\n)+)',
            wrapper,
        )
        assert guard is not None, "deploy/t3 no longer guards the host-identity compose overlay"
        assert "HOST_IDENTITY_FILE" in guard.group("body")
        assert "HOST_IDENTITY_MOUNT=1" in guard.group("body"), (
            "the identity MOUNT PAIR must be admitted by the same conditional that adds the "
            "identity compose file — otherwise the translation claims a root the container "
            "does not carry (on the box) or refuses one it does (off-box)"
        )
        assert wrapper.count("HOST_IDENTITY_MOUNT=1") == 1, (
            "a second setter would let the pair be admitted without the compose overlay"
        )

    def test_the_refusal_still_names_the_identity_root_it_now_translates(self, tmp_path: Path, home: Path) -> None:
        """A checkout outside every root is still refused, and the advertised set is complete."""
        fork = _build_fork(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        checkout = tmp_path / "host-only-clone"
        (checkout / ".git").mkdir(parents=True)

        proc = _run(fork, home, checkout)

        assert proc.returncode != 0
        listed = {line.strip() for line in proc.stderr.splitlines()}
        assert str(home / "workspace") in listed
