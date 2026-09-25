# test-path: cross-cutting — drives deploy/{Dockerfile,entrypoint.sh,docker-compose.yml,t3} (no src mirror).
"""ONE GPG home per container, agreed by every venue — the keybox-lock class.

THE OUTAGE, measured. The container bind-mounts the host's ``~/.gnupg``, and the image
used to bake ``GNUPGHOME`` at that mount. ``deploy/entrypoint.sh`` corrected the value to
a container-local copy, but an exported variable reaches only the exporting process tree:
a ``docker exec``, the loop's own subprocesses, and the ``t3`` wrapper each started from
the container's create-time environment and opened the HOST home instead. Two ``keyboxd``
daemons in two PID namespaces then owned one keybox (host pid 78571, container pid
28533), plus an orphaned container pair left by an earlier restart. gpg's dotlock records
``pid`` + ``hostname``, so ``public-keys.d/pubring.db.lock`` named a pid the host cannot
resolve and a hostname the container cannot match — neither side would judge it stale, so
both waited forever. ``git commit -S`` timed out, ``pass show gitlab/pat`` never returned,
and every ``t3`` GitLab write failed with an empty credential.

The class is NOT "the lock is stale" (it was not — the process was alive; deleting it
would have corrupted a keybox under a running process). The class is **two venues in one
container disagreeing about which home they are opening**. Patching venues one at a time
— a login-shell ``/etc/profile.d`` hook, an inline wrapper prologue — left every
unpatched one broken, which is why the fix is structural: the image bakes ONE
container-local ``GNUPGHOME``, and the entrypoint decides only what that fixed path is
made of.

These tests pin the invariant across all four deploy artifacts at once, because no single
file can hold it: the Dockerfile bakes the value, the entrypoint seeds it, compose backs
it, and the wrapper must keep its hands off it.

THE IMAGE ALONE IS NOT ENOUGH, and that is the second outage. ``deploy.sh`` cannot
converge on macOS (no ``flock``, so it exits 0 having done nothing), which makes a stale
image the STANDING condition rather than an accident: the structural fix merged at 11:50Z
onto a box whose worker image had been built at 08:38Z the same day, and for five days
every ``docker exec`` went on starting from the host mount that image still baked. So
compose pins the value too — create-time environment, inherited by every ``docker exec``
and applied by ``up -d`` with no rebuild.

Three copies of one string is deliberate, and each covers a venue the others cannot: the
Dockerfile ENV covers a bare ``docker run``, the entrypoint's assignment covers the tree
it seeds, and compose covers every stack container whatever its image age. What keeps
three copies honest is the three-way equality below, not a comment.
"""

import json
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
DOCKERFILE = DEPLOY / "Dockerfile"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
COMPOSE_FILE = DEPLOY / "docker-compose.yml"
WRAPPER = DEPLOY / "t3"
_BASH = shutil.which("bash") or "bash"

CONTAINER_HOME = "/home/teatree"
HOST_GNUPG_TARGET = f"{CONTAINER_HOME}/.gnupg"
HISTORICAL_ENTRYPOINT_COMMIT = "e130133337b4d1bceb47ff92a3edb621a4430685"

#: Per-venue GPG-home repairs this branch retired. Naming one anywhere under `deploy/` —
#: code OR runbook — points the next operator at a mechanism that no longer exists.
RETIRED_GNUPG_NAMES = ("resolve_gnupg_home", "10-teatree-gnupg-home", "profile-gnupg-home.sh")


def _dockerfile_env(name: str) -> str:
    """The value the image BAKES for *name* — what a `docker exec` starts from."""
    match = re.search(rf"^\s*(?:ENV\s+)?{re.escape(name)}=(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} is not baked in {DOCKERFILE}"
    return match.group(1)


#: The parameter expansion the entrypoint and compose both write the value through, and
#: what a venue with no override resolves it to.
RUNTIME_DIR_EXPANSION = "${TEATREE_GNUPG_RUNTIME_DIR:-/home/teatree/.gnupg-run}"
RUNTIME_DIR_DEFAULT = "/home/teatree/.gnupg-run"


def _expanded(value: str) -> str:
    """*value* with the runtime-dir override resolved to its default, as an unset venue sees it."""
    return value.replace(RUNTIME_DIR_EXPANSION, RUNTIME_DIR_DEFAULT)


def _entrypoint_container_home() -> str:
    """The path ``deploy/entrypoint.sh`` seeds, with its default expanded."""
    match = re.search(r'^CONTAINER_GNUPG_HOME="(.+)"$', ENTRYPOINT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"CONTAINER_GNUPG_HOME is not assigned in {ENTRYPOINT}"
    return _expanded(match.group(1))


def _compose_service_environments() -> dict[str, dict[str, str]]:
    """Each service's ``environment`` mapping, with YAML merge keys applied.

    Applying them is the point: a value written into a shared anchor's ``environment`` is
    SHADOWED outright by any service declaring its own, because a merge key replaces rather
    than deep-merges — a file that reads as pinned while pinning nothing.
    """
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    return {name: dict(block.get("environment") or {}) for name, block in compose["services"].items()}


def _rendered_services(overrides: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """What the compose renderer hands the daemon — interpolation included, no daemon needed.

    The home comes from the passwd database, not ``$HOME``: the suite isolates ``$HOME``
    per test, and the docker CLI discovers its ``compose`` plugin under that directory —
    so an inherited ``$HOME`` makes the CLI reject ``-f`` as an unknown top-level flag.
    ``TEATREE_GNUPG_RUNTIME_DIR`` is withheld so the render resolves the default spelling
    the Dockerfile bakes rather than an operator's override.
    """
    argv = ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"]
    env = {key: value for key, value in os.environ.items() if key != "TEATREE_GNUPG_RUNTIME_DIR"}
    env.update(overrides or {})
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    proc = subprocess.run(argv, capture_output=True, text=True, check=True, env=env)
    return json.loads(proc.stdout)["services"]


def _rendered_service_environments() -> dict[str, dict[str, str]]:
    return {name: dict(block.get("environment") or {}) for name, block in _rendered_services().items()}


def _shell_function(source: str, name: str) -> str:
    lines = source.splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def _historical_entrypoint() -> str:
    outer_root = DEPLOY.parents[2]
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(
        [
            git,
            "show",
            f"{HISTORICAL_ENTRYPOINT_COMMIT}:vendor/teatree/deploy/entrypoint.sh",
        ],
        cwd=outer_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _compose_mounts() -> list[dict[str, object]]:
    """Every long-form mount in the file — service blocks and YAML anchors alike."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    blocks = [*compose.get("services", {}).values(), *(v for v in compose.values() if isinstance(v, dict))]
    return [volume for block in blocks for volume in (block.get("volumes") or []) if isinstance(volume, dict)]


class TestTheBakedValueIsTheContainerLocalHome:
    """A `docker exec` inherits ONLY what the image baked — so that is what must be right."""

    def test_the_baked_gnupghome_is_not_the_host_mount(self) -> None:
        # The whole outage in one assertion: while these were equal, every venue the
        # entrypoint's export could not reach opened the shared host keybox.
        assert _dockerfile_env("GNUPGHOME") != HOST_GNUPG_TARGET

    def test_the_baked_gnupghome_is_not_inside_the_host_mount(self) -> None:
        # A subdirectory would still be on the bind mount, and a lock taken there is
        # still a lock on a filesystem the host owns.
        assert not _dockerfile_env("GNUPGHOME").startswith(f"{HOST_GNUPG_TARGET}/")

    def test_the_host_home_is_baked_as_a_named_source_not_as_gnupghome(self) -> None:
        # The entrypoint still has to find the key material; naming it explicitly is
        # what lets GNUPGHOME move off it without the seed losing its source.
        assert _dockerfile_env("TEATREE_HOST_GNUPG_DIR") == HOST_GNUPG_TARGET

    def test_the_baked_value_is_exactly_what_the_entrypoint_seeds(self) -> None:
        # Two spellings of one path is the drift that recreates the class: the exec'd
        # process would open a home the entrypoint never built.
        assert _dockerfile_env("GNUPGHOME") == _entrypoint_container_home()


class TestAllThreeVenuesSpellTheHomeIdentically:
    """The Dockerfile, the entrypoint and compose each cover a venue the others cannot.

    Redundancy is the design; DRIFT between the copies is the defect, and only a test can
    tell them apart. The compose copy is what makes correctness independent of image age —
    the property that matters on a box whose converger cannot run.
    """

    def test_every_stack_service_pins_the_home_the_image_bakes(self) -> None:
        baked = _dockerfile_env("GNUPGHOME")
        pinned = {name: _expanded(env.get("GNUPGHOME", "")) for name, env in _compose_service_environments().items()}
        assert pinned, f"no services parsed out of {COMPOSE_FILE}"
        assert {name: value for name, value in pinned.items() if value != baked} == {}

    def test_the_pin_survives_yaml_merge_into_the_rendered_config(self) -> None:
        # A merge key does not deep-merge, so a pin written only into the shared anchor
        # renders away silently. This asserts on what the daemon is actually handed.
        if shutil.which("docker") is None:
            pytest.skip("needs the docker CLI to render the compose config (no daemon required)")
        baked = _dockerfile_env("GNUPGHOME")
        rendered = {name: env.get("GNUPGHOME", "") for name, env in _rendered_service_environments().items()}
        assert rendered, f"no services rendered out of {COMPOSE_FILE}"
        assert {name: value for name, value in rendered.items() if value != baked} == {}

    def test_the_runtime_directory_override_reaches_every_service_and_mount(self) -> None:
        if shutil.which("docker") is None:
            pytest.skip("needs the docker CLI to render the compose config (no daemon required)")
        runtime = "/var/run/teatree-gnupg"
        services = _rendered_services({"TEATREE_GNUPG_RUNTIME_DIR": runtime})
        assert services
        for block in services.values():
            environment = block.get("environment") or {}
            assert environment.get("TEATREE_GNUPG_RUNTIME_DIR") == runtime
            assert environment.get("GNUPGHOME") == f"{runtime}/gnupg"
        tmpfs_targets = {
            volume["target"]
            for block in services.values()
            for volume in block.get("volumes") or []
            if volume.get("type") == "tmpfs"
        }
        assert tmpfs_targets == {runtime}


class TestComposeRunsTheCurrentEntrypoint:
    def test_every_service_uses_the_entrypoint_from_the_deploy_checkout(self) -> None:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        expected = [
            "bash",
            "${TEATREE_DEPLOY_CHECKOUT:-/home/teatree/teatree-deploy}/deploy/entrypoint.sh",
        ]
        assert {name: block.get("entrypoint") for name, block in compose["services"].items()} == dict.fromkeys(
            compose["services"], expected
        )

    def test_the_rendered_entrypoint_tracks_a_relocated_deploy_checkout(self) -> None:
        if shutil.which("docker") is None:
            pytest.skip("needs the docker CLI to render the compose config (no daemon required)")
        checkout = "/srv/teatree-current"
        services = _rendered_services({"TEATREE_DEPLOY_CHECKOUT": checkout})
        assert {name: block.get("entrypoint") for name, block in services.items()} == {
            name: ["bash", f"{checkout}/deploy/entrypoint.sh"] for name in services
        }

    def test_the_historical_entrypoint_leaves_an_empty_create_time_home_unseeded(self, tmp_path: Path) -> None:
        source = _historical_entrypoint()
        script = "\n".join(
            [
                "set -euo pipefail",
                *(
                    _shell_function(source, name)
                    for name in (
                        "path_fstype",
                        "fstype_hosts_unix_sockets",
                        "derive_container_gnupg_home",
                        "resolve_gnupg_home",
                    )
                ),
                "resolve_gnupg_home",
                'printf "%s" "$GNUPGHOME"',
            ]
        )
        runtime = tmp_path / "empty-tmpfs"
        home = runtime / "gnupg"
        proc = subprocess.run(
            [_BASH, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={
                "GNUPGHOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "TEATREE_GNUPG_RUNTIME_DIR": str(runtime),
            },
        )
        assert proc.stdout == str(home)
        assert not home.exists()


class TestComposeBacksBothPaths:
    def test_the_host_gpg_home_is_bind_mounted_at_the_named_source(self) -> None:
        sources = {m["target"] for m in _compose_mounts() if m.get("type") == "bind"}
        assert HOST_GNUPG_TARGET in sources

    def test_the_container_local_home_sits_on_a_tmpfs(self) -> None:
        # It holds a COPY of the host's PRIVATE KEY material on a sharing-transport
        # host, so it belongs in RAM and must vanish with the container.
        tmpfs_targets = {_expanded(str(m["target"])) for m in _compose_mounts() if m.get("type") == "tmpfs"}
        parent = str(Path(_dockerfile_env("GNUPGHOME")).parent)
        assert parent in tmpfs_targets


class TestNoOtherVenueResolvesAGpgHome:
    """Per-venue repair IS the class. Exactly one file may decide the home."""

    def test_only_the_entrypoint_assigns_gnupghome(self) -> None:
        offenders: list[str] = []
        for path in sorted(DEPLOY.rglob("*")):
            if not path.is_file() or path in {ENTRYPOINT, DOCKERFILE}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            offenders += [
                f"{path.name}: {line.strip()}"
                for line in text.splitlines()
                if line.strip().startswith(("GNUPGHOME=", "export GNUPGHOME", "GNUPGHOME ="))
            ]
        assert not offenders, f"a second venue re-deriving the GPG home is the defect class: {offenders}"

    def test_the_retired_login_shell_hook_is_gone(self) -> None:
        assert not (DEPLOY / "profile-gnupg-home.sh").exists()

    def test_no_deploy_file_still_documents_a_retired_per_venue_repair(self) -> None:
        """Deleting the hook does not delete the runbook that sends an operator looking for it.

        The assignment scan above only matches a ``GNUPGHOME=`` line START, which prose never
        is — so ``deploy/README.md`` went on describing the retired login-shell hook and the
        renamed resolver as the live design with every gate green.
        """
        offenders: list[str] = []
        for path in sorted(DEPLOY.rglob("*")):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            offenders += [
                f"{path.name}:{number}: {line.strip()}"
                for number, line in enumerate(lines, start=1)
                for name in RETIRED_GNUPG_NAMES
                if name in line
            ]
        assert not offenders, f"a retired GPG-home venue is still documented as live: {offenders}"

    def test_the_wrapper_carries_no_gpg_home_predicate(self) -> None:
        assignments = [
            line.strip()
            for line in WRAPPER.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("GNUPGHOME=", "export GNUPGHOME"))
        ]
        assert not assignments, f"deploy/t3 must not re-derive a GPG home: {assignments}"


class TestOnASharingTransportNoContainerProcessTouchesTheHostKeybox:
    """The behavioural end of it, run against the entrypoint's REAL shell functions."""

    _FUNCTIONS = (
        "path_fstype",
        "fstype_hosts_unix_sockets",
        "derive_container_gnupg_home",
        "seed_container_gnupg_home",
    )

    def _seed(self, tmp_path: Path, host: Path, runtime: Path, fstype: str) -> str:
        source = ENTRYPOINT.read_text(encoding="utf-8")
        parts: list[str] = [line for line in source.splitlines() if line.startswith("CONTAINER_GNUPG_HOME=")]
        for name in self._FUNCTIONS:
            body: list[str] = []
            capturing = False
            for line in source.splitlines():
                if line.startswith(f"{name}() {{"):
                    capturing = True
                if capturing:
                    body.append(line)
                    if line == "}":
                        break
            assert body, f"function {name!r} not found in {ENTRYPOINT}"
            parts.append("\n".join(body))
        table = tmp_path / "mounts"
        table.write_text(f"/dev/src / ext4 rw 0 0\n/dev/src {host} {fstype} rw 0 0\n", encoding="utf-8")
        harness = tmp_path / "harness.sh"
        harness.write_text(
            "set -euo pipefail\n"
            + "\n".join(parts)
            + '\nseed_container_gnupg_home >/dev/null\nprintf "%s" "$GNUPGHOME"\n',
            encoding="utf-8",
        )
        proc = subprocess.run(
            [_BASH, str(harness)],
            capture_output=True,
            text=True,
            check=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "TEATREE_PROC_MOUNTS": str(table),
                "TEATREE_GNUPG_RUNTIME_DIR": str(runtime),
                "TEATREE_HOST_GNUPG_DIR": str(host),
                # The value a `docker exec` would inherit if the image still baked the
                # host mount — the seed must ignore it, not honour it.
                "GNUPGHOME": str(host),
            },
        )
        return proc.stdout.strip()

    @staticmethod
    def _host_gnupg(home: Path) -> None:
        home.mkdir(parents=True)
        (home / "common.conf").write_text("use-keyboxd\n", encoding="utf-8")
        (home / "private-keys-v1.d").mkdir()
        (home / "private-keys-v1.d" / "ABC123.key").write_bytes(b"secret-key")
        (home / "public-keys.d").mkdir()
        (home / "public-keys.d" / "pubring.db").write_bytes(b"keyboxd-db")
        # The live lock the outage turned on: a pid the OTHER namespace cannot resolve.
        (home / "public-keys.d" / "pubring.db.lock").write_text("78571\n", encoding="utf-8")

    def test_the_seeded_home_is_a_copy_that_shares_no_keybox_with_the_host(self, tmp_path: Path) -> None:
        host, runtime = tmp_path / "host", tmp_path / "run"
        self._host_gnupg(host)
        resolved = Path(self._seed(tmp_path, host, runtime, "fakeowner"))

        assert resolved == runtime / "gnupg"
        assert not resolved.is_symlink(), "a symlink here puts the container back on the host keybox"
        assert (resolved / "public-keys.d" / "pubring.db").read_bytes() == b"keyboxd-db"
        assert resolved.resolve() != host.resolve()

    def test_the_hosts_live_dotlock_never_travels_into_the_copy(self, tmp_path: Path) -> None:
        # It names a pid this namespace cannot resolve, so gpg would wait on it forever
        # — and it must be left ALONE on the host, where the process holding it is alive.
        host, runtime = tmp_path / "host", tmp_path / "run"
        self._host_gnupg(host)
        resolved = Path(self._seed(tmp_path, host, runtime, "fakeowner"))

        assert not (resolved / "public-keys.d" / "pubring.db.lock").exists()
        assert (host / "public-keys.d" / "pubring.db.lock").read_text(encoding="utf-8") == "78571\n"

    def test_an_inherited_host_gnupghome_is_overridden_not_honoured(self, tmp_path: Path) -> None:
        # The exec'd-process case: whatever it started with, the seed lands it on the
        # one container-local home. Before the fix this returned the host path.
        host, runtime = tmp_path / "host", tmp_path / "run"
        self._host_gnupg(host)
        assert Path(self._seed(tmp_path, host, runtime, "fakeowner")) != host

    def test_a_socket_capable_host_keeps_the_deliberate_in_place_sharing(self, tmp_path: Path) -> None:
        # The deployment box, where sharing is the POINT: one home means one gpg-agent,
        # which is what makes the cached-passphrase setup deploy/README.md documents
        # work. The isolation this file pins is one home per CONTAINER, not a ban on
        # the box's single-tenant sharing.
        host, runtime = tmp_path / "host", tmp_path / "run"
        self._host_gnupg(host)
        resolved = Path(self._seed(tmp_path, host, runtime, "ext4"))

        assert resolved == runtime / "gnupg"
        assert resolved.is_symlink()
        assert resolved.resolve() == host.resolve()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
