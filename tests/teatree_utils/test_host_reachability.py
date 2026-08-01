"""Naming the Docker host from wherever the CLI happens to be running.

`localhost` means two different machines depending on who asks. Run natively it is
the host that published the port; run inside the containerized CLI it is the
container's own loopback, where nothing is listening. Every consumer of a
host-published port has to ask which name reaches the host from HERE.
"""

from pathlib import Path
from unittest.mock import patch

import teatree.utils.ports as ports_mod
from teatree.utils.ports import host_published_port_host, running_in_container


class TestRunningInContainer:
    def test_the_docker_marker_file_is_conclusive(self) -> None:
        with patch.object(Path, "exists", return_value=True):
            assert running_in_container() is True

    def test_a_containerd_cgroup_counts_even_without_the_marker(self) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "read_text", return_value="0::/system.slice/containerd.service\n"),
        ):
            assert running_in_container() is True

    def test_a_plain_host_is_not_a_container(self) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "read_text", return_value="0::/user.slice/user-1000.slice\n"),
        ):
            assert running_in_container() is False

    def test_an_unreadable_cgroup_is_not_a_container(self) -> None:
        """Absence of /proc (macOS, Windows) is not evidence of a container."""
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "read_text", side_effect=OSError("no /proc")),
        ):
            assert running_in_container() is False


class TestHostPublishedPortHost:
    def test_a_native_run_keeps_localhost(self) -> None:
        """The overwhelmingly common case must not change, on any OS."""
        with patch.object(ports_mod, "running_in_container", return_value=False):
            assert host_published_port_host() == "localhost"

    def test_a_container_prefers_the_alias_that_resolves(self) -> None:
        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.socket, "gethostbyname", return_value="192.168.65.2") as resolve,
        ):
            assert host_published_port_host() == "host.docker.internal"
        resolve.assert_called_once_with("host.docker.internal")

    def test_a_container_falls_through_to_the_next_alias(self) -> None:
        unresolvable = "NXDOMAIN"

        def resolve(name: str) -> str:
            if name == "host.docker.internal":
                raise OSError(unresolvable)
            return "172.17.0.1"

        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.socket, "gethostbyname", side_effect=resolve),
        ):
            assert host_published_port_host() == "gateway.docker.internal"

    def test_a_linux_container_with_no_aliases_uses_the_bridge_gateway(self) -> None:
        """A plain Linux daemon publishes neither alias; the default bridge still routes."""
        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.socket, "gethostbyname", side_effect=OSError("NXDOMAIN")),
        ):
            assert host_published_port_host() == "172.17.0.1"
