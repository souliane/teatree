"""Proving a host path is reachable inside the CLI container, before claiming it.

``deploy/t3`` refuses a working directory it cannot translate into container
coordinates, and documents ``TEATREE_INVOCATION_CWD`` as the escape for a caller
that KNOWS the tree is reachable. These lanes are what turns "knows" into a proof:
a path under an identity bind mount is handed across, and everything else — a
translating mount, an unmounted tree, a daemon that will not answer — yields
nothing, so the refusal stands.

The negative lanes are the point. A prover that always says yes is a blind
``TEATREE_INVOCATION_CWD=$(pwd)``, which is exactly the leak the refusal exists to
stop.
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from hooks.scripts import container_visibility

_MOUNT_LISTING = "/srv/host/teatree-deploy\t/srv/host/teatree-deploy\n/srv/host/.local/bin\t/var/lib/teatree/host-bin\n"


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr="")


@contextmanager
def _docker_answers(*stdouts: str):
    """Patch the docker probe to answer each call in turn, cache cleared around it."""
    container_visibility.identity_mount_roots.cache_clear()
    try:
        with patch.object(container_visibility, "_docker", side_effect=list(stdouts)) as docker:
            yield docker
    finally:
        container_visibility.identity_mount_roots.cache_clear()


class TestIdentityMountRoots:
    def test_a_mount_landing_on_its_own_path_is_a_root(self) -> None:
        with _docker_answers("abc123 \n", _MOUNT_LISTING):
            assert container_visibility.identity_mount_roots() == ("/srv/host/teatree-deploy",)

    def test_a_translating_mount_is_not(self) -> None:
        """`/srv/host/.local/bin` is real inside the container — under another name."""
        with _docker_answers("abc123 \n", _MOUNT_LISTING):
            assert "/srv/host/.local/bin" not in container_visibility.identity_mount_roots()

    def test_an_ephemeral_one_off_container_is_skipped(self) -> None:
        """Compose lists a `run --rm` under the service name; `exec` never lands there."""
        with _docker_answers("oneoff1 True\nservice1 \n", _MOUNT_LISTING) as docker:
            container_visibility.identity_mount_roots()
        assert docker.call_args.args[-1] == "service1"

    def test_a_daemon_that_will_not_answer_proves_nothing(self) -> None:
        with _docker_answers(None):
            assert container_visibility.identity_mount_roots() == ()

    def test_an_unreadable_mount_table_proves_nothing(self) -> None:
        with _docker_answers("abc123 \n", None):
            assert container_visibility.identity_mount_roots() == ()

    def test_no_running_service_container_proves_nothing(self) -> None:
        with _docker_answers("oneoff1 True\n"):
            assert container_visibility.identity_mount_roots() == ()


class TestDockerProbe:
    def test_an_absent_docker_binary_answers_nothing(self) -> None:
        with patch.object(container_visibility.shutil, "which", return_value=None):
            assert container_visibility._docker("ps") is None

    def test_a_failed_probe_answers_nothing(self) -> None:
        with (
            patch.object(container_visibility.shutil, "which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=_completed("boom", returncode=1)),
        ):
            assert container_visibility._docker("ps") is None

    def test_a_probe_that_never_returns_answers_nothing(self) -> None:
        with (
            patch.object(container_visibility.shutil, "which", return_value="/usr/bin/docker"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 5)),
        ):
            assert container_visibility._docker("ps") is None


class TestContainerPath:
    def test_a_checkout_under_an_identity_mount_crosses_unchanged(self) -> None:
        with _docker_answers("abc123 \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/teatree-deploy")) == ("/srv/host/teatree-deploy")

    def test_a_tree_nested_under_one_crosses_too(self) -> None:
        with _docker_answers("abc123 \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/teatree-deploy/hooks")) == (
                "/srv/host/teatree-deploy/hooks"
            )

    def test_an_unmounted_tree_is_not_vouched_for(self) -> None:
        with _docker_answers("abc123 \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/elsewhere/repo")) is None

    def test_a_tree_under_a_translating_mount_is_not_vouched_for(self) -> None:
        """It IS readable inside — under a different name, which is not what we hand over."""
        with _docker_answers("abc123 \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/.local/bin/t3")) is None

    def test_it_vouches_for_the_physical_path_a_mount_source_is(self, tmp_path) -> None:
        real = (tmp_path / "real").resolve()
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with _docker_answers("abc123 \n", f"{real}\t{real}\n"):
            assert container_visibility.container_path(link) == str(real)
