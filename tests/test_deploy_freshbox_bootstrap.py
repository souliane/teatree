"""Fresh-box bootstrap invariants for the headless deploy substrate.

Three failure modes broke a first deploy onto a clean box and are pinned here
against the deploy files (the source of truth):

- ``deploy/deploy.sh`` must pre-create EVERY host bind-mount source owned by the
    deploy user. A source missing at ``up`` time is auto-created by dockerd
    ROOT-owned, and the non-root container then cannot write it.
- The container's runtime UID must equal the HOST deploy user's UID so every
    path-identity bind mount is writable: ``deploy.sh`` derives it from the host
    (``id -u``) and passes it through ``deploy/docker-compose.yml`` into the
    ``deploy/Dockerfile`` ``TEATREE_UID`` build arg, which defaults to 1001 (the
    live box's deploy user) when nothing exports it.
- ``deploy/Dockerfile`` must digest-pin its base image to the same manifest that
    ``dev/Dockerfile.test`` pins, so a floating-tag retag cannot change the
    toolchain silently.
"""

import re
from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
COMPOSE_FILE = DEPLOY_DIR / "docker-compose.yml"
DEPLOY_SH = DEPLOY_DIR / "deploy.sh"
CLI_WRAPPER = DEPLOY_DIR / "t3"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
DEV_DOCKERFILE = Path(__file__).resolve().parents[1] / "dev" / "Dockerfile.test"

_HOME = "/home/teatree"  # privacy-scan:allow — the box's public, documented deploy home
# The host-side root every compose bind SOURCE is written against. Both scripts
# that create those dirs set it from `$HOME`, which on the box IS `_HOME`.
_HOST_HOME_PLACEHOLDER = "${TEATREE_HOST_HOME:-" + _HOME + "}"
# The deploy checkout mount's source — the checkout each script runs out of, not a
# dir either creates (#4120).
_DEPLOY_CHECKOUT_PLACEHOLDER = "${TEATREE_DEPLOY_CHECKOUT:-" + _HOME + "/teatree-deploy}"
# The agent-scratch sweep's host-namespace pair (#4165) — kernel-guaranteed paths
# on any POSIX host, never absent, so dockerd's auto-create-as-root hazard cannot
# apply to either.
_HOST_SCRATCH_PLACEHOLDER = "${TEATREE_HOST_TMP:-/tmp}"
_HOST_PROC_SOURCE = "/proc"


def _bind_sources() -> set[str]:
    """Every host bind-mount SOURCE path the shared service list declares.

    Sources are written as ``${TEATREE_HOST_HOME:-/home/teatree}/<suffix>``; the
    placeholder resolves to the box's deploy home so the paths compare directly
    against the ``$HOME``-rooted dirs the scripts pre-create.

    Three sources are excluded, each because the pre-create rule's premise —
    "an absent source is auto-created ROOT-owned" — cannot fire for it:

    - the deploy checkout: not a state dir either script creates but the
    directory each is EXECUTING FROM, so it exists by construction. Pinned by
    :class:`TestDeployCheckoutSourceIsTheCheckoutTheScriptsRunFrom` below.
    - the host temp root and the host process table: kernel-guaranteed on any
    POSIX host (``/tmp``, or its override, and ``/proc`` are never absent), so
    neither is ever auto-created. Pinned by
    :class:`TestHostNamespaceSourcesAreKernelGuaranteed` below.
    """
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    volumes = compose["x-teatree-common"]["volumes"]
    return {
        entry["source"].replace(_HOST_HOME_PLACEHOLDER, _HOME, 1)
        for entry in volumes
        if isinstance(entry, dict) and entry.get("type") == "bind"
    } - {_DEPLOY_CHECKOUT_PLACEHOLDER, _HOST_SCRATCH_PLACEHOLDER, _HOST_PROC_SOURCE}


def _created_dirs(script: str, command: str, var: str) -> set[str]:
    """The absolute paths *script* pre-creates with *command*, rooted at *var*.

    Backslash line continuations are folded first so a multi-line invocation
    reads as one command; the host-home variable expands to the deploy user's
    home to match the compose bind sources.
    """
    folded = re.sub(r"\\\n\s*", " ", script)
    pattern = re.compile(r'"\$' + re.escape(var) + r'(/[^"]+)"')
    targets: set[str] = set()
    for line in folded.splitlines():
        if not line.strip().startswith(command):
            continue
        targets.update(_HOME + suffix for suffix in pattern.findall(line))
    return targets


def _install_d_targets(script: str) -> set[str]:
    """The absolute paths ``deploy.sh`` pre-creates via ``install -d``."""
    return _created_dirs(script, "install -d", "HOME")


def _wrapper_mount_source_arrays() -> dict[str, set[str]]:
    """The host paths each ``*_MOUNT_SOURCES`` array in ``deploy/t3`` declares.

    The wrapper names its bind sources ONCE, in arrays, because the same list
    answers both "pre-create these" and "can the container see this path?" — the
    unreachable-path diagnostic reads them, so the two cannot drift. Only arrays
    an ``install -d`` line actually expands are returned, so a dead array cannot
    satisfy the invariant below.
    """
    text = CLI_WRAPPER.read_text(encoding="utf-8")
    pattern = re.compile(r'"\$TEATREE_HOST_HOME(/[^"]+)"')
    install_lines = [line for line in text.splitlines() if line.strip().startswith("install -d")]
    return {
        name: {_HOME + suffix for suffix in pattern.findall(body)}
        for name, body in re.findall(r"^(\w+_MOUNT_SOURCES)=\(\n(.*?)^\)$", text, re.MULTILINE | re.DOTALL)
        if any(f'"${{{name}[@]}}"' in line for line in install_lines)
    }


def _base_digest(dockerfile_text: str) -> str | None:
    match = re.search(r"ubuntu:24\.04@(sha256:[0-9a-f]{64})", dockerfile_text)
    return match.group(1) if match else None


class TestDeployPreCreatesEveryBindSource:
    def test_all_bind_sources_are_pre_created_owned_by_deploy_user(self) -> None:
        created = _install_d_targets(DEPLOY_SH.read_text(encoding="utf-8"))
        missing = _bind_sources() - created
        assert not missing, f"deploy.sh does not pre-create bind sources: {sorted(missing)}"

    def test_credential_plane_is_created_mode_700(self) -> None:
        folded = re.sub(r"\\\n\s*", " ", DEPLOY_SH.read_text(encoding="utf-8"))
        secret_lines = [
            line for line in folded.splitlines() if line.strip().startswith("install -d") and ".password-store" in line
        ]
        assert secret_lines, "pass store must be pre-created"
        for line in secret_lines:
            assert "-m 700" in line, "credential-plane dirs must be created mode 700"

    def test_deploy_exports_the_host_home_the_mounts_read(self) -> None:
        # The dirs created above are rooted at `$HOME`; the mounts are rooted at
        # `$TEATREE_HOST_HOME`. Exporting one from the other is what makes them
        # the same dirs by construction instead of by coincidence.
        assert 'export TEATREE_HOST_HOME="$HOME"' in DEPLOY_SH.read_text(encoding="utf-8")


class TestCliWrapperPreCreatesEveryBindSource:
    """Off-box, `deploy/t3` is the entry point — it owns the same invariant.

    dockerd refuses a bind whose source is absent on the host ("mounts denied"),
    so the wrapper that starts a one-off container must create the sources too;
    on the box it is a no-op next to `deploy.sh`.
    """

    def test_all_bind_sources_are_pre_created(self) -> None:
        arrays = _wrapper_mount_source_arrays()
        created = set().union(*arrays.values()) if arrays else set()
        missing = _bind_sources() - created
        assert not missing, f"deploy/t3 does not pre-create bind sources: {sorted(missing)}"

    def test_credential_plane_is_created_mode_700(self) -> None:
        arrays = _wrapper_mount_source_arrays()
        secret_arrays = [name for name, paths in arrays.items() if any(p.endswith("/.password-store") for p in paths)]
        assert secret_arrays, "pass store must be pre-created"
        folded = re.sub(r"\\\n\s*", " ", CLI_WRAPPER.read_text(encoding="utf-8"))
        install_lines = [line for line in folded.splitlines() if line.strip().startswith("install -d")]
        for name in secret_arrays:
            expanding = [line for line in install_lines if f'"${{{name}[@]}}"' in line]
            assert expanding, f"{name} must be passed to install -d"
            for line in expanding:
                assert "-m 700" in line, "credential-plane dirs must be created mode 700"


class TestDeployCheckoutSourceIsTheCheckoutTheScriptsRunFrom:
    """What replaces the pre-create rule for the one bind source nobody creates.

    The pre-create invariant exists because dockerd auto-creates an absent bind
    source ROOT-owned. The deploy checkout can never be absent: both entry points
    derive it from their own ``$REPO_ROOT``, the directory holding the script
    being executed. Pinning that derivation is what keeps the exclusion in
    :func:`_bind_sources` honest rather than a hole (#4120).
    """

    def test_deploy_sh_exports_it_from_its_own_repo_root(self) -> None:
        assert 'export TEATREE_DEPLOY_CHECKOUT="$REPO_ROOT"' in DEPLOY_SH.read_text(encoding="utf-8")

    def test_cli_wrapper_exports_it_from_its_own_repo_root(self) -> None:
        assert 'export TEATREE_DEPLOY_CHECKOUT="${TEATREE_DEPLOY_CHECKOUT:-$REPO_ROOT}"' in CLI_WRAPPER.read_text(
            encoding="utf-8"
        )

    def test_the_compose_source_reads_that_same_variable(self) -> None:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        sources = {
            entry["source"]
            for entry in compose["x-teatree-common"]["volumes"]
            if isinstance(entry, dict) and entry.get("type") == "bind"
        }
        assert _DEPLOY_CHECKOUT_PLACEHOLDER in sources


class TestHostNamespaceSourcesAreKernelGuaranteed:
    """What replaces the pre-create rule for the scratch sweep's host-namespace pair.

    ``/tmp`` (or its ``TEATREE_HOST_TMP`` override) and ``/proc`` are never
    absent on a POSIX host — the kernel provides both before any userspace
    process, including dockerd, runs. The auto-create-as-root hazard the
    pre-create rule guards against needs an ABSENT source; neither of these can
    ever be one, so excluding them from :func:`_bind_sources` is not a gap, it is
    the correct answer to a premise that cannot fire (#4165).
    """

    def test_the_compose_sources_are_exactly_the_kernel_guaranteed_pair(self) -> None:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        sources = {
            entry["source"]
            for entry in compose["x-teatree-common"]["volumes"]
            if isinstance(entry, dict) and entry.get("type") == "bind"
        }
        assert _HOST_SCRATCH_PLACEHOLDER in sources
        assert _HOST_PROC_SOURCE in sources

    def test_the_process_table_source_is_not_home_rooted(self) -> None:
        # /proc is a virtual kernel filesystem, never a directory under a deploy
        # user's home — asserting this keeps the exemption from silently
        # widening to cover a real, creatable state dir later.
        assert not _HOST_PROC_SOURCE.startswith(_HOME)


class TestHostDerivedRuntimeUid:
    def test_uid_is_a_build_arg_defaulting_to_1001(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^ARG TEATREE_UID=1001$", text, re.MULTILINE), (
            "container UID must be a build arg defaulting to 1001 (the live box's deploy user)"
        )

    def test_user_is_renumbered_to_the_arg_uid(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r'usermod\b[^\n]*-u\s+"\$\{TEATREE_UID\}"[^\n]*\bteatree\b', text), (
            "teatree user must be renumbered onto the TEATREE_UID build arg"
        )
        assert re.search(r'groupmod\b[^\n]*-g\s+"\$\{TEATREE_UID\}"[^\n]*\bteatree\b', text), (
            "teatree primary group must track the TEATREE_UID build arg"
        )

    def test_stock_ubuntu_user_is_removed_to_free_uid_1000(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"userdel\b[^\n]*\bubuntu\b", text), (
            "Ubuntu 24.04's stock 'ubuntu' user (UID 1000) must be removed first"
        )

    def test_deploy_sh_derives_uid_from_the_host_deploy_user(self) -> None:
        text = DEPLOY_SH.read_text(encoding="utf-8")
        assert re.search(r'TEATREE_UID="\$\(id -u', text), (
            "deploy.sh must derive the container UID from the host deploy user via `id -u`"
        )
        assert re.search(r"\bexport TEATREE_UID\b", text), (
            "deploy.sh must export TEATREE_UID so compose reads it into the build arg"
        )
        assert re.search(r"TEATREE_UID=1001\b", text), (
            "deploy.sh must fall back to 1001 (the live box's deploy user) if derivation fails"
        )
        # No hardcoded 1000 default may sneak back in — that would break the live box.
        assert not re.search(r"TEATREE_UID=1000\b", text), "deploy.sh must not hardcode UID 1000"

    def test_compose_plumbs_the_uid_build_arg_defaulting_to_1001(self) -> None:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        anchor_args = compose["x-teatree-common"]["build"]["args"]
        assert anchor_args["TEATREE_UID"] == "${TEATREE_UID:-1001}", (
            "the shared build must pass TEATREE_UID through, defaulting to 1001"
        )
        watchdog_args = compose["services"]["teatree-watchdog"]["build"]["args"]
        assert watchdog_args["TEATREE_UID"] == "${TEATREE_UID:-1001}", (
            "the standalone watchdog build shares the image and must pass the same UID arg"
        )


class TestBaseImageDigestPin:
    def test_deploy_base_is_digest_pinned(self) -> None:
        assert _base_digest(DOCKERFILE.read_text(encoding="utf-8")) is not None, (
            "deploy/Dockerfile FROM must be pinned by @sha256 digest"
        )

    def test_deploy_base_digest_matches_dev_dockerfile(self) -> None:
        deploy_digest = _base_digest(DOCKERFILE.read_text(encoding="utf-8"))
        dev_digest = _base_digest(DEV_DOCKERFILE.read_text(encoding="utf-8"))
        assert dev_digest is not None, "dev/Dockerfile.test must digest-pin its base"
        assert deploy_digest == dev_digest, "deploy and dev must pin the same ubuntu:24.04 manifest"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
