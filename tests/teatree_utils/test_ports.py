"""Docker host resolution and shared-network checks."""

from unittest.mock import patch

from teatree.utils import ports as ports_mod
from teatree.utils.ports import docker_host_address


class TestDockerHostAddress:
    """Which side of the container boundary the caller is on."""

    def test_stock_linux_host_has_no_alias_so_the_bridge_gateway_is_the_host(self) -> None:
        with patch.object(ports_mod, "running_in_container", return_value=False):
            assert docker_host_address() == "172.17.0.1"

    def test_inside_a_container_the_alias_is_probed_from_the_daemon_resolver(self) -> None:
        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.socket, "gethostbyname", return_value="192.168.65.2"),
        ):
            assert docker_host_address() == "host.docker.internal"

    def test_inside_a_container_with_no_alias_falls_back_to_the_bridge_gateway(self) -> None:
        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.socket, "gethostbyname", side_effect=OSError),
        ):
            assert docker_host_address() == "172.17.0.1"
