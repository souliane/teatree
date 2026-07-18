"""Fresh-box bootstrap invariants for the headless deploy substrate.

Three failure modes broke a first deploy onto a clean box and are pinned here
against the deploy files (the source of truth):

- ``deploy/deploy.sh`` must pre-create EVERY host bind-mount source owned by the
    deploy user. A source missing at ``up`` time is auto-created by dockerd
    ROOT-owned, and the non-root container then cannot write it.
- ``deploy/Dockerfile`` must give its runtime user a DETERMINISTIC UID (equal to
    the host deploy user) so every path-identity bind mount is writable.
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
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
DEV_DOCKERFILE = Path(__file__).resolve().parents[1] / "dev" / "Dockerfile.test"

_HOME = "/home/teatree"  # privacy-scan:allow — the box's public, documented deploy home


def _bind_sources() -> set[str]:
    """Every host bind-mount SOURCE path the shared service list declares."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    volumes = compose["x-teatree-common"]["volumes"]
    return {entry["source"] for entry in volumes if isinstance(entry, dict) and entry.get("type") == "bind"}


def _install_d_targets(script: str) -> set[str]:
    """The absolute paths ``deploy.sh`` pre-creates via ``install -d``.

    Backslash line continuations are folded first so a multi-line ``install -d``
    reads as one command; ``$HOME`` expands to the deploy user's home to match the
    compose bind sources.
    """
    folded = re.sub(r"\\\n\s*", " ", script)
    targets: set[str] = set()
    for line in folded.splitlines():
        if not line.strip().startswith("install -d"):
            continue
        targets.update(_HOME + suffix for suffix in re.findall(r'"\$HOME(/[^"]+)"', line))
    return targets


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


class TestDeterministicRuntimeUid:
    def test_uid_is_a_build_arg_defaulting_to_1000(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^ARG TEATREE_UID=1000$", text, re.MULTILINE), (
            "container UID must be a deterministic build arg defaulting to 1000"
        )

    def test_user_is_renumbered_to_the_arg_uid(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r'usermod\b[^\n]*-u\s+"\$\{TEATREE_UID\}"[^\n]*\bteatree\b', text), (
            "teatree user must be renumbered onto the deterministic TEATREE_UID"
        )
        assert re.search(r'groupmod\b[^\n]*-g\s+"\$\{TEATREE_UID\}"[^\n]*\bteatree\b', text), (
            "teatree primary group must track the deterministic TEATREE_UID"
        )

    def test_stock_ubuntu_user_is_removed_to_free_uid_1000(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"userdel\b[^\n]*\bubuntu\b", text), (
            "Ubuntu 24.04's stock 'ubuntu' user (UID 1000) must be removed first"
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
