"""Who may drive the docker daemon, and where the watchdog's checkout lives.

Two deploy invariants that were each broken in a way nothing reported.

`worktree provision` shells out to `docker build` and `worktree start` to
`compose up`, so **teatree-worker** cannot do its job without the daemon. Mounting
the socket is not enough on its own — it is mode `0660` root-owned while every app
service runs as the non-root `TEATREE_UID` — so the grant is the worker's
`group_add`, and the fact that only the worker declares one is what keeps the
capability at one service even though the mount sits in the shared set.

**teatree-watchdog** bind-mounts the deploy checkout read-only at PATH IDENTITY
(source == target) so its inner `compose up -d` hashes identically to the host's.
Hard-coding the box path made that mount unsatisfiable anywhere else — dockerd
refuses a source the host does not have — which left the supervisor permanently
`Created` on any non-box host. Both sides now read one variable, so identity holds
by construction and the box keeps its historical path as the default.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_ENTRYPOINT = _DEPLOY / "entrypoint.sh"
_DEPLOY_SH = _DEPLOY / "deploy.sh"
_T3 = _DEPLOY / "t3"

#: The session's REAL home, captured at import — before the suite's fixtures redirect
#: ``$HOME`` into a sandbox. ``docker compose`` is a CLI PLUGIN resolved out of
#: ``~/.docker/cli-plugins``, so under the redirected home the subcommand does not
#: exist and docker rejects the invocation ("unknown shorthand flag: 'f'"). That
#: rendered the golden below a permanent silent skip on a host that has docker.
_REAL_HOME = os.environ.get("HOME", "")

#: Absolute interpreter / CLI paths, resolved once. Absolute so the subprocess calls
#: below name a full executable rather than leaning on the child's PATH.
_BASH = shutil.which("bash") or "/bin/bash"
_DOCKER = shutil.which("docker")

_SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"
_GID_PLACEHOLDER = "${TEATREE_DOCKER_SOCKET_GID:-0}"
_CHECKOUT_PLACEHOLDER = "${TEATREE_DEPLOY_CHECKOUT:-/home/teatree/teatree-deploy}"
#: The on-box checkout, kept as the default so a box deploy needs nothing exported.
_BOX_CHECKOUT = "/home/teatree/teatree-deploy"

#: The app services sharing the `&teatree-common` anchor — and therefore the socket
#: mount. Only one of them may hold the group that makes the mount usable.
_APP_SERVICES = ("teatree-init", "teatree-worker", "teatree-admin", "teatree-slack-listener")

#: The GID resolver, duplicated verbatim in both deploy entry points because each is
#: copied and run standalone. Matched from `if [ -z` to the closing `export`.
_GID_BLOCK = re.compile(
    r'if \[ -z "\$\{TEATREE_DOCKER_SOCKET_GID:-\}" \];.*?export TEATREE_DOCKER_SOCKET_GID',
    re.DOTALL,
)


def _compose() -> dict:
    # SafeLoader resolves `&teatree-common` and the `<<` merge keys, so each service's
    # effective mount list is what that role actually runs with.
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _service(name: str) -> dict:
    return _compose()["services"][name]


class TestWorkerCanDriveTheDaemon:
    """The service that runs provisioning reaches dockerd — mount AND grant."""

    def test_worker_mounts_the_socket(self) -> None:
        assert _SOCKET_MOUNT in _service("teatree-worker")["volumes"], (
            "teatree-worker runs `worktree provision` (docker build) and `worktree start` "
            "(compose up); without the socket both die on 'failed to connect to the docker API'"
        )

    def test_worker_is_granted_the_socket_group(self) -> None:
        # The mount alone grants nothing: the socket is 0660 root-owned and the worker
        # runs as the non-root TEATREE_UID. The supplementary group IS the grant.
        assert _service("teatree-worker")["group_add"] == [_GID_PLACEHOLDER]

    def test_only_the_worker_holds_that_grant(self) -> None:
        # The mount is shared (one anchor), the capability is not. Every other app
        # service sees an unopenable socket node, which is the intended posture.
        granted = [name for name in _APP_SERVICES if "group_add" in _service(name)]
        assert granted == ["teatree-worker"]

    def test_worker_keeps_its_non_root_uid(self) -> None:
        # A `user: "0:0"` shortcut would break the path-identity UID invariant on every
        # bind mount, and is broader than the group the worker actually needs.
        assert "user" not in _service("teatree-worker")

    def test_the_socket_is_writable(self) -> None:
        # The docker API is a socket: a read-only bind refuses the write half of
        # every request, so the mount must never grow a `:ro` suffix.
        mounts = [m for m in _service("teatree-worker")["volumes"] if isinstance(m, str)]
        assert [m for m in mounts if m.startswith("/var/run/docker.sock:")] == [_SOCKET_MOUNT]


class TestProvisioningToolchainIsInTheImage:
    """A tool the worker SHELLS OUT to by name must ship in the image."""

    def test_the_postgres_client_is_installed(self) -> None:
        # `worktree provision`'s DB-import strategy runs `createdb` / `pg_restore`
        # by bare name. Absent, provisioning dies on `FileNotFoundError: 'createdb'`
        # — a host-only assumption exactly like the socket one above.
        dockerfile = (_DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        assert "postgresql-client" in dockerfile

    def test_the_docker_cli_and_its_plugins_are_installed(self) -> None:
        # `docker build` and `docker compose up` are likewise resolved by name out
        # of the worker. buildx is not optional: an overlay base image whose
        # Dockerfile uses `--mount=type=cache` needs BuildKit, and BuildKit without
        # the buildx component fails outright while the legacy builder rejects the
        # mount syntax — leaving the build no working path at all.
        dockerfile = (_DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        for package in ("docker-ce-cli", "docker-compose-plugin", "docker-buildx-plugin"):
            assert package in dockerfile


class TestWatchdogCheckoutIsHostPortable:
    """The supervisor's read-only checkout mount resolves on box AND laptop."""

    def _checkout_bind(self) -> dict:
        binds = [
            mount
            for mount in _service("teatree-watchdog")["volumes"]
            if isinstance(mount, dict) and mount.get("target") == _CHECKOUT_PLACEHOLDER
        ]
        assert len(binds) == 1
        return binds[0]

    def test_bind_source_and_target_are_the_same_expression(self) -> None:
        # Path identity is what keeps the watchdog's inner `compose up -d` from
        # recreating the stack. Reading ONE variable on both sides makes that true
        # on any host instead of only where the checkout sits at the box path.
        bind = self._checkout_bind()
        assert bind["source"] == bind["target"] == _CHECKOUT_PLACEHOLDER

    def test_the_checkout_stays_read_only(self) -> None:
        assert self._checkout_bind()["read_only"] is True

    def test_the_box_path_survives_as_the_default(self) -> None:
        assert _BOX_CHECKOUT in _CHECKOUT_PLACEHOLDER

    def test_no_hard_coded_box_checkout_remains_in_the_compose_file(self) -> None:
        # A second literal would silently reintroduce the unmountable path.
        literals = [
            line
            for line in _COMPOSE.read_text(encoding="utf-8").splitlines()
            if _BOX_CHECKOUT in line and _CHECKOUT_PLACEHOLDER not in line and not line.lstrip().startswith("#")
        ]
        assert literals == []

    def test_the_checkout_is_handed_to_the_container(self) -> None:
        # The entrypoint execs `$TEATREE_DEPLOY_CHECKOUT/deploy/watchdog.sh`, so the
        # value must reach the container's environment, not just the mount spec.
        assert _service("teatree-watchdog")["environment"]["TEATREE_DEPLOY_CHECKOUT"] == _CHECKOUT_PLACEHOLDER

    def test_the_host_interpolation_environment_is_forwarded(self) -> None:
        # The watchdog repairs by running an inner `compose up -d` over THIS file,
        # and compose interpolates from ITS environment. Without these it renders
        # the in-file defaults and any service it recreates comes back wired to the
        # wrong source tree / home / clone dir — a corruption, not a repair.
        environment = _service("teatree-watchdog")["environment"]
        assert environment["TEATREE_HOST_HOME"] == "${TEATREE_HOST_HOME:-/home/teatree}"
        assert environment["TEATREE_SOURCE_MOUNT"] == "${TEATREE_SOURCE_MOUNT:-teatree_src}"
        assert environment["TEATREE_CLONE_DIR"] == "${TEATREE_CLONE_DIR:-/home/teatree/teatree}"
        assert environment["TEATREE_UID"] == "${TEATREE_UID:-1001}"
        assert environment["TEATREE_DOCKER_SOCKET_GID"] == _GID_PLACEHOLDER

    def test_forwarded_defaults_match_the_ones_the_services_resolve(self) -> None:
        # The forward is only a no-op on the box if each default is IDENTICAL to the
        # one the shared anchor and services already carry. A drifted default would
        # make the watchdog quietly deploy a different stack than the host did.
        compose_text = _COMPOSE.read_text(encoding="utf-8")
        environment = _service("teatree-watchdog")["environment"]
        for name in ("TEATREE_HOST_HOME", "TEATREE_SOURCE_MOUNT", "TEATREE_CLONE_DIR", "TEATREE_UID"):
            placeholder = environment[name]
            assert compose_text.count(placeholder) >= 2, (
                f"{name}: the watchdog's forwarded default must be the same expression the services use"
            )

    def test_entrypoint_execs_the_watchdog_from_that_checkout(self) -> None:
        text = _ENTRYPOINT.read_text(encoding="utf-8")
        assert f'exec bash "{_CHECKOUT_PLACEHOLDER}/deploy/watchdog.sh" --loop' in text
        assert f"exec bash {_BOX_CHECKOUT}/deploy/watchdog.sh" not in text


class TestBothEntryPointsExportTheEnvironment:
    """`deploy.sh` (box) and `deploy/t3` (laptop) each hand compose the same values."""

    @pytest.mark.parametrize("script", [_DEPLOY_SH, _T3], ids=["deploy.sh", "t3"])
    def test_exports_the_deploy_checkout_from_its_own_repo_root(self, script: Path) -> None:
        assert 'TEATREE_DEPLOY_CHECKOUT="' in script.read_text(encoding="utf-8")
        assert "$REPO_ROOT" in script.read_text(encoding="utf-8")

    @pytest.mark.parametrize("script", [_DEPLOY_SH, _T3], ids=["deploy.sh", "t3"])
    def test_resolves_and_exports_the_socket_gid(self, script: Path) -> None:
        # deploy/t3 needs it too: with the stack down it starts a one-off worker itself.
        assert _GID_BLOCK.search(script.read_text(encoding="utf-8")) is not None

    def test_the_two_resolvers_are_byte_identical(self) -> None:
        # Deliberate duplication — each script is copied and run standalone, so a
        # sourced sibling would be a new way for either to die. This pins the copies.
        blocks = [_GID_BLOCK.search(p.read_text(encoding="utf-8")) for p in (_DEPLOY_SH, _T3)]
        assert all(blocks)
        assert blocks[0].group(0) == blocks[1].group(0)


class TestSocketGidResolution:
    """The resolver runs for real, out of the entry point that carries it."""

    def _resolve(self, env_extra: dict[str, str] | None = None) -> str:
        block = _GID_BLOCK.search(_T3.read_text(encoding="utf-8"))
        assert block is not None
        script = f'set -eu\n{block.group(0)}\nprintf %s "$TEATREE_DOCKER_SOCKET_GID"'
        completed = subprocess.run(
            [_BASH, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, **(env_extra or {})},
            timeout=30,
            check=True,
        )
        return completed.stdout.strip()

    def test_it_resolves_a_numeric_gid(self) -> None:
        assert self._resolve().isdigit()

    def test_an_operator_value_always_wins(self) -> None:
        assert self._resolve({"TEATREE_DOCKER_SOCKET_GID": "4242"}) == "4242"


@pytest.mark.skipif(_DOCKER is None, reason="docker CLI not available")
class TestRenderedComposeConfig:
    """What `docker compose` actually resolves — the golden the services start from."""

    def _config(self, checkout: str) -> dict:
        completed = subprocess.run(
            [_DOCKER or "", "compose", "-f", str(_COMPOSE), "config"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "HOME": _REAL_HOME,
                "TEATREE_DEPLOY_CHECKOUT": checkout,
                "TEATREE_DOCKER_SOCKET_GID": "0",
            },
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"docker compose config unavailable: {completed.stderr.strip()[:200]}")
        return yaml.safe_load(completed.stdout)

    def test_an_off_box_checkout_renders_at_path_identity(self, tmp_path: Path) -> None:
        checkout = str(tmp_path / "somewhere-else")
        watchdog = self._config(checkout)["services"]["teatree-watchdog"]
        binds = [m for m in watchdog["volumes"] if m.get("target") == checkout]
        assert len(binds) == 1
        assert binds[0]["source"] == checkout

    def test_the_worker_renders_the_socket_and_its_group(self, tmp_path: Path) -> None:
        worker = self._config(str(tmp_path))["services"]["teatree-worker"]
        assert "/var/run/docker.sock" in {m.get("target") for m in worker["volumes"]}
        assert worker["group_add"] == ["0"]
