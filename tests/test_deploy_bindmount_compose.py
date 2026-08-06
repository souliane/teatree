"""The headless stack externalizes its state onto host bind mounts.

The factory's DB, worktrees, and workspaces live on the HOST (not in Docker
named volumes), so the container and the host converge on ONE db.sqlite3. The
credential plane (the host pass store + its GPG home) is a further dedicated
bind mount, decoupled from the data dir so a data-dir change can never orphan
the provisioned credential store again (the #3262 regression).

Each mount's TARGET is fixed at the canonical container path under
``HOME=/home/teatree`` — what ``teatree.paths`` resolves inside the container
(``deploy/Dockerfile`` sets no ``XDG_DATA_HOME``). The SOURCE is that same path
rebased onto ``${TEATREE_HOST_HOME:-/home/teatree}``, the host home carrying the
state tree: on the box the deploy user's home IS ``/home/teatree``, so source ==
target and path identity holds; off-box (an operator laptop whose home is not
``/home/teatree``) the sources follow the real host home, which is what makes
the stack mountable there at all — dockerd refuses a source path the host does
not have.

The source tree the container executes is the same knob in mount form:
``${TEATREE_SOURCE_MOUNT:-teatree_src}`` defaults to the box's self-updating
named volume and takes a host directory to run a working tree instead.

Structure is parsed from the YAML directly (the source of truth); golden
`docker compose config` assertions render both the default and the overridden
host home when a usable docker is present.
"""

import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "deploy" / "docker-compose.yml"

# HOME inside the container — every mount TARGET is anchored here.
CONTAINER_HOME = "/home/teatree"
# The host-side root of every bind SOURCE, and its on-box default.
HOST_HOME_PLACEHOLDER = "${TEATREE_HOST_HOME:-/home/teatree}"
DEFAULT_HOST_HOME = CONTAINER_HOME
# The source-tree mount: the box's self-updating clone volume by default.
SOURCE_MOUNT_PLACEHOLDER = "${TEATREE_SOURCE_MOUNT:-teatree_src}"
DEFAULT_SOURCE_VOLUME = "teatree_src"
CONTAINER_SOURCE_DIR = f"{CONTAINER_HOME}/teatree"

# The three externalized state mounts, by canonical container target.
EXTERNALIZED = {
    f"{CONTAINER_HOME}/.local/share/teatree",
    f"{CONTAINER_HOME}/.local/share/teatree-worktrees",
    f"{CONTAINER_HOME}/workspace/t3-workspaces",
}
# The credential plane: the host pass store + its GPG home, DEDICATED bind mounts
# decoupled from the data dir so a data-dir change can never orphan the
# provisioned credential store again (#3262 regression).
CREDENTIAL_PLANE = {
    f"{CONTAINER_HOME}/.password-store",
    f"{CONTAINER_HOME}/.gnupg",
}
# The Claude session plane: the dream pass's transcript input + memory-corpus
# product. Without it the containerized dream pass globs an empty projects dir
# and every nightly consolidation is a permanent no-op.
SESSION_PLANE = {
    f"{CONTAINER_HOME}/.claude/projects",
}
# Every host bind mount the shared list must carry, by canonical container target.
ALL_BIND_TARGETS = EXTERNALIZED | CREDENTIAL_PLANE | SESSION_PLANE
# The deploy checkout: the clone `workspace ticket` cuts worktrees from (#4120).
# A different KIND of bind from the state planes above — it is not a container
# path rebased onto the host home but the SAME path on both sides, so the
# absolute `gitdir:` a worktree records resolves in either venue.
DEPLOY_CHECKOUT_PLACEHOLDER = (
    "${TEATREE_DEPLOY_CHECKOUT:-/home/teatree/teatree-deploy}"  # privacy-scan:allow — public deploy home
)
# The mounts that stay Docker-managed named volumes by default.
#: ``teatree_control_db`` holds the control database itself — a named volume so the
#: file has no host path for a host process to open (teatree.db.write_domain).
#: ``teatree_clones`` is the container's own CLONE root. Deliberately NOT a bind of
#: the host's ``~/workspace``: a git worktree records an absolute ``gitdir`` pointer
#: into its source clone, so a clone is shareable only where both venues name it
#: identically — which the host home variable cannot guarantee for a whole root.
#: Sharing is done per-clone instead, by a discovery symlink into the deploy
#: checkout bound at path identity (#4120). A volume rather than the image layer so
#: the clones survive container recreation.
CLONE_ROOT_VOLUME = "teatree_clones"
CLONE_ROOT_TARGET = f"{CONTAINER_HOME}/workspace"
KEPT_NAMED_VOLUMES = {DEFAULT_SOURCE_VOLUME, "teatree_uv", "teatree_control_db", CLONE_ROOT_VOLUME}
REMOVED_NAMED_VOLUMES = {"teatree_data", "teatree_worktrees", "teatree_workspaces"}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _common_volumes() -> list:
    """The shared mount list every service inherits via `*teatree-common`."""
    return _compose()["x-teatree-common"]["volumes"]


def _render(work_dir: Path, env_overrides: dict[str, str]) -> dict:
    """`docker compose config` for an isolated copy, under a controlled env."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker not available for the golden config render")
    # Isolated copy with a stub env_file so the real box secrets are never read;
    # `config` neither builds nor starts anything.
    compose_copy = work_dir / "docker-compose.yml"
    compose_copy.write_text(COMPOSE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    (work_dir / "teatree.env").write_text("T3_DEBUG=0\n", encoding="utf-8")
    # The interpolation knobs come ONLY from env_overrides — a developer shell
    # that already exports them must not change what this golden renders.
    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_HOST_HOME", "TEATREE_SOURCE_MOUNT"}}
    env.update(env_overrides)
    # `compose` is a CLI PLUGIN the docker binary loads from the config dir, which
    # defaults to $HOME/.docker — and conftest redirects HOME to a throwaway
    # sandbox, so the plugin would be unfindable and every golden here would
    # degrade to a skip. Resolve the config dir from the passwd database (immune
    # to the HOME redirect) when the caller has not pinned one.
    env.setdefault("DOCKER_CONFIG", str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker"))
    proc = subprocess.run(
        [docker, "compose", "-f", str(compose_copy), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config unusable here: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def _rendered_mounts(rendered: dict) -> dict:
    return {m["target"]: m for svc in rendered["services"].values() for m in svc.get("volumes", [])}


def _short_syntax_sources() -> set[str]:
    """Named-volume sources of the `SOURCE:TARGET` short-syntax mounts.

    Split on the LAST colon: a source may itself carry one (compose's
    `${VAR:-default}` interpolation), while the target is an absolute container
    path that cannot.

    An ABSOLUTE source is a bind by compose's own short-syntax rule, not a named
    volume — the docker socket the worker drives the daemon through is one — so it
    is excluded here rather than counted as a volume this file forgot to declare.
    """
    return {
        source
        for source in (entry.rsplit(":", 1)[0] for entry in _common_volumes() if isinstance(entry, str))
        if not source.startswith("/")
    }


def _resolve_default(source: str) -> str:
    """The value a `${VAR:-default}` source renders to when VAR is unset."""
    if source.startswith("${") and ":-" in source:
        return source.rpartition(":-")[2].removesuffix("}")
    return source


class TestExternalizedBindMounts:
    def _bind_mounts(self) -> dict:
        return {
            entry["target"]: entry
            for entry in _common_volumes()
            if isinstance(entry, dict) and entry.get("type") == "bind"
        }

    def test_state_dirs_are_bind_mounts_at_canonical_container_targets(self) -> None:
        binds = self._bind_mounts()
        assert set(binds) == ALL_BIND_TARGETS | {DEPLOY_CHECKOUT_PLACEHOLDER}, (
            "every state + credential dir must be a host bind mount, plus the deploy checkout"
        )

    def test_every_state_bind_source_is_the_target_rebased_on_the_host_home(self) -> None:
        # The target is fixed (it is what `teatree.paths` resolves inside the
        # container); only the host-side root varies, through ONE variable whose
        # default keeps source == target on the box.
        binds = self._bind_mounts()
        for target in ALL_BIND_TARGETS:
            suffix = target.removeprefix(CONTAINER_HOME)
            assert binds[target]["source"] == f"{HOST_HOME_PLACEHOLDER}{suffix}", (
                f"{target}: source must be the target rebased on {HOST_HOME_PLACEHOLDER}"
            )

    def test_the_deploy_checkout_is_bound_at_path_identity_and_writable(self) -> None:
        # `git worktree add` bakes an ABSOLUTE `gitdir:` into its source clone, so
        # only source == target makes the recorded pointer resolve in both venues;
        # writable because that same add writes `.git/worktrees/<n>` into the clone.
        entry = self._bind_mounts()[DEPLOY_CHECKOUT_PLACEHOLDER]
        assert entry["source"] == DEPLOY_CHECKOUT_PLACEHOLDER == entry["target"]
        assert not entry.get("read_only", False)

    def test_credential_plane_is_a_dedicated_bind_mount(self) -> None:
        # The pass store + GPG home must be their own mounts (not nested under the
        # data dir), so externalizing/moving the data dir never orphans them again.
        binds = self._bind_mounts()
        assert set(binds) >= CREDENTIAL_PLANE, "pass store + GPG home must be host bind mounts"
        for target in CREDENTIAL_PLANE:
            assert not target.startswith(f"{CONTAINER_HOME}/.local/share/teatree"), (
                f"{target}: credential plane must be decoupled from the data dir"
            )

    def test_kept_named_volume_mounts_still_present(self) -> None:
        assert {_resolve_default(source) for source in _short_syntax_sources()} == KEPT_NAMED_VOLUMES

    def test_no_state_dir_uses_a_named_volume_mount(self) -> None:
        assert _short_syntax_sources().isdisjoint(REMOVED_NAMED_VOLUMES), "state dirs must not mount named volumes"


class TestSourceTreeMountIsOverridable:
    """The container's source tree: box volume by default, host tree on demand."""

    def _source_entry(self) -> str:
        entries = [
            entry
            for entry in _common_volumes()
            if isinstance(entry, str) and entry.endswith(f":{CONTAINER_SOURCE_DIR}")
        ]
        assert len(entries) == 1, f"exactly one mount must serve {CONTAINER_SOURCE_DIR}"
        return entries[0]

    def test_source_mount_defaults_to_the_self_updating_named_volume(self) -> None:
        # The box's runtime clone lives on this volume and the entrypoint
        # fast-forwards it from origin; a bind default would break self-update.
        assert self._source_entry() == f"{SOURCE_MOUNT_PLACEHOLDER}:{CONTAINER_SOURCE_DIR}"

    def test_cli_wrapper_points_the_source_mount_at_a_vendored_core_checkout(self) -> None:
        # `deploy/t3` invoked from `<fork>/vendor/teatree/deploy/t3` must run THAT
        # working tree, so an edit on the host changes what the container executes.
        wrapper = (COMPOSE_FILE.parent / "t3").read_text(encoding="utf-8")
        assert "TEATREE_SOURCE_MOUNT" in wrapper
        assert 'export TEATREE_HOST_HOME="${TEATREE_HOST_HOME:-$HOME}"' in wrapper


class TestDockerComposeConfigGolden:
    """Golden: `docker compose config` resolves the same mounts end to end."""

    def test_default_host_home_keeps_path_identity(self, tmp_path: Path) -> None:
        mounts = _rendered_mounts(_render(tmp_path, {}))
        for target in ALL_BIND_TARGETS:
            assert target in mounts, f"{target} missing from rendered config"
            assert mounts[target]["type"] == "bind"
            # On the box the deploy user's home IS the container home.
            assert mounts[target]["source"] == target.replace(CONTAINER_HOME, DEFAULT_HOST_HOME, 1)

    def test_default_source_mount_is_the_named_volume(self, tmp_path: Path) -> None:
        mount = _rendered_mounts(_render(tmp_path, {}))[CONTAINER_SOURCE_DIR]
        assert mount["type"] == "volume"
        assert mount["source"] == DEFAULT_SOURCE_VOLUME

    def test_host_home_override_moves_sources_and_keeps_targets(self, tmp_path: Path) -> None:
        host_home = "/home/operator"
        mounts = _rendered_mounts(_render(tmp_path, {"TEATREE_HOST_HOME": host_home}))
        for target in ALL_BIND_TARGETS:
            assert mounts[target]["type"] == "bind"
            assert mounts[target]["source"] == target.replace(CONTAINER_HOME, host_home, 1)

    def test_source_mount_override_binds_a_host_working_tree(self, tmp_path: Path) -> None:
        source = "/srv/downstream-fork/vendor/teatree"
        mount = _rendered_mounts(_render(tmp_path, {"TEATREE_SOURCE_MOUNT": source}))[CONTAINER_SOURCE_DIR]
        assert mount["type"] == "bind"
        assert mount["source"] == source


class TestContainerOwnedCloneRoot:
    """The clone root is the container's own volume, with the worktree bind inside it.

    A git worktree records an absolute ``gitdir`` into its source clone, so a clone
    is usable on both sides only where the two venues name it identically; bound at
    a DIFFERENT path it reads as ``fatal: not a git repository``. The
    root therefore stays container-owned, and the one clone worktrees are cut from
    is shared per-clone through the path-identity checkout mount (#4120), while the
    worktrees themselves stay host-visible for reading and editing.
    """

    def test_clone_root_is_a_named_volume_at_the_canonical_workspace_path(self, tmp_path: Path) -> None:
        mount = _rendered_mounts(_render(tmp_path, {}))[CLONE_ROOT_TARGET]
        assert mount["type"] == "volume"
        assert mount["source"] == CLONE_ROOT_VOLUME

    def test_clone_root_is_never_a_host_bind(self, tmp_path: Path) -> None:
        # Binding the operator's ~/workspace is the fix that does NOT work: off-box
        # the host root differs from the container's, so every gitdir pointer would
        # still name a path the other side cannot resolve.
        for env in ({}, {"TEATREE_HOST_HOME": "/home/operator"}):
            assert _rendered_mounts(_render(tmp_path, env))[CLONE_ROOT_TARGET]["type"] == "volume"

    def test_worktree_root_bind_nests_inside_the_clone_root(self, tmp_path: Path) -> None:
        # Docker orders mounts by target depth, so the volume lands first and this
        # bind lands on top of it — clones container-only, worktrees host-visible.
        worktree_root = f"{CONTAINER_HOME}/workspace/t3-workspaces"
        assert worktree_root.startswith(f"{CLONE_ROOT_TARGET}/")
        assert _rendered_mounts(_render(tmp_path, {}))[worktree_root]["type"] == "bind"


class TestTopLevelVolumeDeclarations:
    def test_unused_named_volume_declarations_removed(self) -> None:
        declared = set(_compose().get("volumes") or {})
        assert declared == KEPT_NAMED_VOLUMES
        assert declared.isdisjoint(REMOVED_NAMED_VOLUMES)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
