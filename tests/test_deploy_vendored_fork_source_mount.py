"""A vendored fork must run its OWN root in the container, not core alone.

Overlays register through the HOST project's ``teatree.overlays`` entry point,
so the containerized ``t3`` resolves a downstream overlay only when the fork
root is installed alongside core. ``deploy/entrypoint.sh`` does that install
(``--with-editable "$HOST_ROOT"``) but derives ``HOST_ROOT`` from the layout it
can SEE: ``$TEATREE_CLONE_DIR`` must end in ``/vendor/teatree`` with a
``pyproject.toml`` at its parent. Mounting only ``<fork>/vendor/teatree`` gives
the container no fork root at all, so the detection can never fire and every
headless task on an overlay-owned ticket dies at dispatch with
``Overlay '<name>' not found. Available: t3-teatree``.

``deploy/t3`` therefore mounts the FORK ROOT at the fixed container source path
and points ``TEATREE_CLONE_DIR`` at the vendored subpath beneath it, which is
the exact shape the entrypoint's ``*/vendor/teatree`` case matches. A STANDALONE
core clone must keep the self-updating ``teatree_src`` volume and set neither
variable — that is the box, and it is the regression this file guards hardest.

The wrapper is exercised for real: a fake fork tree under ``tmp_path`` with the
genuine ``deploy/t3`` copied in, and a ``docker`` stub on PATH that reports the
mount wiring it was handed. Docker is the one unstoppable external here.
"""

import json
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
WRAPPER = DEPLOY_DIR / "t3"
COMPOSE_FILE = DEPLOY_DIR / "docker-compose.yml"

# The container path the source mount always lands on (compose target, fixed).
CONTAINER_SOURCE_DIR = "/home/teatree/teatree"
# Where core sits under it once the FORK ROOT is what gets mounted — the shape
# entrypoint.sh's `*/vendor/teatree` HOST_ROOT case matches.
VENDORED_CORE_IN_CONTAINER = f"{CONTAINER_SOURCE_DIR}/vendor/teatree"
# What the compose services must interpolate, so an operator override wins and
# the unset (box) case renders the image's own default.
CLONE_DIR_PLACEHOLDER = f"${{TEATREE_CLONE_DIR:-{CONTAINER_SOURCE_DIR}}}"
# The services that mount the source tree and therefore need the clone dir; the
# watchdog is deliberately excluded (it mounts no source and runs no install).
APP_SERVICES = frozenset({"teatree-init", "teatree-worker", "teatree-admin", "teatree-slack-listener"})

UNSET = "<unset>"

# `compose ps` answers "nothing running" so the wrapper takes its one-off `run`
# branch; any other invocation reports the wiring the wrapper exported.
DOCKER_STUB = f"""#!/usr/bin/env bash
for arg in "$@"; do
    [ "$arg" = ps ] && exit 0
done
printf 'TEATREE_SOURCE_MOUNT=%s\\n' "${{TEATREE_SOURCE_MOUNT-{UNSET}}}"
printf 'TEATREE_CLONE_DIR=%s\\n' "${{TEATREE_CLONE_DIR-{UNSET}}}"
"""


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _install_wrapper(deploy_dir: Path) -> Path:
    deploy_dir.mkdir(parents=True, exist_ok=True)
    entry = deploy_dir / "t3"
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


def _build_vendored_fork(root: Path, *, with_pyproject: bool = True) -> Path:
    fork = root / "downstream-fork"
    _install_wrapper(fork / "vendor" / "teatree" / "deploy")
    if with_pyproject:
        (fork / "pyproject.toml").write_text('[project]\nname = "downstream-fork"\n', encoding="utf-8")
    return fork


def _invoke(entry: Path, home: Path, env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Run the wrapper against a stub docker; return the wiring it exported."""
    stub_dir = home / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    docker = stub_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_SOURCE_MOUNT", "TEATREE_CLONE_DIR"}}
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["TEATREE_HOST_HOME"] = str(home)
    env.update(env_overrides or {})

    # Stand OUTSIDE any checkout: the subject is the source-mount wiring, and
    # inheriting pytest's cwd would instead trip the invisible-checkout refusal
    # (this repo is a checkout, and `TEATREE_HOST_HOME` is redirected above so it
    # sits under none of the mounts the wrapper computes).
    elsewhere = home / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run([str(entry), "--help"], capture_output=True, text=True, check=True, env=env, cwd=elsewhere)
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


class TestVendoredForkRunsTheWholeFork:
    def test_source_mount_is_the_fork_root_not_the_vendored_core(self, tmp_path: Path) -> None:
        # Mounting core alone leaves the container with no fork root, so the
        # entrypoint's --with-editable target does not exist and no overlay
        # entry point is ever registered.
        fork = _build_vendored_fork(tmp_path)
        wiring = _invoke(fork / "vendor" / "teatree" / "deploy" / "t3", tmp_path / "home")
        assert wiring["TEATREE_SOURCE_MOUNT"] == str(fork.resolve())

    def test_clone_dir_points_at_the_vendored_core_beneath_the_mount(self, tmp_path: Path) -> None:
        # With the fork root on the fixed container source path, core moves one
        # level down — and entrypoint.sh only recognises a fork when CLONE_DIR
        # ends in `/vendor/teatree`.
        fork = _build_vendored_fork(tmp_path)
        wiring = _invoke(fork / "vendor" / "teatree" / "deploy" / "t3", tmp_path / "home")
        assert wiring["TEATREE_CLONE_DIR"] == VENDORED_CORE_IN_CONTAINER

    def test_root_deploy_symlink_reaches_the_same_wiring(self, tmp_path: Path) -> None:
        # A fork commonly exposes `<fork>/deploy -> vendor/teatree/deploy`. Bash
        # resolves `..` LOGICALLY, which rebases the repo root onto the fork and
        # defeats a basename check; physical resolution keeps both entries equal.
        fork = _build_vendored_fork(tmp_path)
        (fork / "deploy").symlink_to(fork / "vendor" / "teatree" / "deploy")
        wiring = _invoke(fork / "deploy" / "t3", tmp_path / "home")
        assert wiring["TEATREE_SOURCE_MOUNT"] == str(fork.resolve())
        assert wiring["TEATREE_CLONE_DIR"] == VENDORED_CORE_IN_CONTAINER

    def test_explicit_source_mount_still_wins(self, tmp_path: Path) -> None:
        pinned = "/srv/some/other/checkout"
        fork = _build_vendored_fork(tmp_path)
        wiring = _invoke(
            fork / "vendor" / "teatree" / "deploy" / "t3",
            tmp_path / "home",
            {"TEATREE_SOURCE_MOUNT": pinned},
        )
        assert wiring["TEATREE_SOURCE_MOUNT"] == pinned
        assert wiring["TEATREE_CLONE_DIR"] == UNSET

    def test_explicit_clone_dir_still_wins(self, tmp_path: Path) -> None:
        pinned = "/home/teatree/teatree/nested/core"
        fork = _build_vendored_fork(tmp_path)
        wiring = _invoke(
            fork / "vendor" / "teatree" / "deploy" / "t3",
            tmp_path / "home",
            {"TEATREE_CLONE_DIR": pinned},
        )
        assert wiring["TEATREE_CLONE_DIR"] == pinned

    def test_vendor_parent_without_a_python_project_keeps_the_core_only_mount(self, tmp_path: Path) -> None:
        # No pyproject.toml at the parent means entrypoint.sh would reject it as
        # a host project too, so there is nothing to gain by mounting it.
        fork = _build_vendored_fork(tmp_path, with_pyproject=False)
        core = fork / "vendor" / "teatree"
        wiring = _invoke(core / "deploy" / "t3", tmp_path / "home")
        assert wiring["TEATREE_SOURCE_MOUNT"] == str(core.resolve())
        assert wiring["TEATREE_CLONE_DIR"] == UNSET


class TestStandaloneCloneIsUntouched:
    """The box: a plain teatree clone keeps the self-updating source volume."""

    def test_plain_clone_sets_neither_variable(self, tmp_path: Path) -> None:
        clone = tmp_path / "teatree-deploy"
        entry = _install_wrapper(clone / "deploy")
        wiring = _invoke(entry, tmp_path / "home")
        assert wiring == {"TEATREE_SOURCE_MOUNT": UNSET, "TEATREE_CLONE_DIR": UNSET}

    def test_clone_named_teatree_outside_a_vendor_dir_sets_neither_variable(self, tmp_path: Path) -> None:
        # `~/workspace/souliane/teatree` — the right basename, the wrong parent.
        clone = tmp_path / "souliane" / "teatree"
        entry = _install_wrapper(clone / "deploy")
        wiring = _invoke(entry, tmp_path / "home")
        assert wiring == {"TEATREE_SOURCE_MOUNT": UNSET, "TEATREE_CLONE_DIR": UNSET}


class TestComposeForwardsTheCloneDir:
    def _service_env(self, name: str) -> dict:
        return _compose()["services"][name].get("environment") or {}

    def test_every_source_mounting_service_forwards_the_clone_dir(self) -> None:
        # The wrapper exports it on the HOST; only an `environment:` entry
        # interpolates it into the container, where the entrypoint reads it.
        for name in sorted(APP_SERVICES):
            assert self._service_env(name).get("TEATREE_CLONE_DIR") == CLONE_DIR_PLACEHOLDER, (
                f"{name} must forward TEATREE_CLONE_DIR to the container"
            )

    def test_the_watchdog_carries_it_only_as_a_pass_through(self) -> None:
        # The watchdog mounts no source tree and runs no install, so the value is
        # never FOR it. It carries the same placeholder the app services do because
        # its repair is an inner `compose up -d` over this file, and compose
        # interpolates from the environment of whoever runs it: without the
        # forward, a service the watchdog has to recreate on a fork comes back
        # pointing at the default clone dir, where the entrypoint finds no vendored
        # core and registers no overlay. A watchdog-SPECIFIC value would be the
        # claim it cannot honour; the pass-through is not.
        assert self._service_env("teatree-watchdog").get("TEATREE_CLONE_DIR") == CLONE_DIR_PLACEHOLDER

    def test_the_unset_default_is_the_canonical_container_source_dir(self) -> None:
        # The box exports nothing, so every service must render the image's own
        # TEATREE_CLONE_DIR — byte-identical to the pre-fork behaviour.
        assert CLONE_DIR_PLACEHOLDER.rpartition(":-")[2].removesuffix("}") == CONTAINER_SOURCE_DIR


class TestComposeGoldenPropagatesTheCloneDir:
    """`docker compose config` proves the host export reaches the container env."""

    def _render(self, work_dir: Path, env_overrides: dict[str, str]) -> dict:
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker not available for the golden config render")
        compose_copy = work_dir / "docker-compose.yml"
        compose_copy.write_text(COMPOSE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        (work_dir / "teatree.env").write_text("T3_DEBUG=0\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "TEATREE_CLONE_DIR"}
        env.update(env_overrides)
        # conftest redirects HOME, which would hide the compose CLI plugin.
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

    def test_exported_clone_dir_reaches_every_source_mounting_service(self, tmp_path: Path) -> None:
        rendered = self._render(tmp_path, {"TEATREE_CLONE_DIR": VENDORED_CORE_IN_CONTAINER})
        for name in sorted(APP_SERVICES):
            assert rendered["services"][name]["environment"]["TEATREE_CLONE_DIR"] == VENDORED_CORE_IN_CONTAINER

    def test_unset_clone_dir_renders_the_box_default(self, tmp_path: Path) -> None:
        rendered = self._render(tmp_path, {})
        for name in sorted(APP_SERVICES):
            assert rendered["services"][name]["environment"]["TEATREE_CLONE_DIR"] == CONTAINER_SOURCE_DIR


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
