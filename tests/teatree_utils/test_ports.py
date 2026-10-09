"""Docker host resolution and shared-network checks."""

from unittest.mock import patch

import pytest

from teatree.utils import ports as ports_mod
from teatree.utils.ports import docker_host_address


class TestDockerHostAddress:
    """Which side of the container boundary the caller is on, never which OS it is.

    Natively the host OS is a sound proxy for the daemon's. Inside a container it
    is not: ``platform.system()`` reports Linux whatever the host is, so a
    containerized CLI on a macOS host returned the bridge gateway — an address
    nothing is listening on there.
    """

    @pytest.mark.parametrize("system", ["Darwin", "Windows"])
    def test_desktop_host_publishes_the_alias(self, system: str) -> None:
        with (
            patch.object(ports_mod, "running_in_container", return_value=False),
            patch.object(ports_mod.platform, "system", return_value=system),
        ):
            assert docker_host_address() == "host.docker.internal"

    def test_stock_linux_host_has_no_alias_so_the_bridge_gateway_is_the_host(self) -> None:
        with (
            patch.object(ports_mod, "running_in_container", return_value=False),
            patch.object(ports_mod.platform, "system", return_value="Linux"),
        ):
            assert docker_host_address() == "172.17.0.1"

    def test_inside_a_container_the_alias_is_probed_not_inferred_from_the_os(self) -> None:
        # The regression: platform.system() reads "Linux" inside the container even
        # on a macOS host, so an OS branch answers with the bridge gateway while the
        # daemon's own resolver answers the alias.
        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.platform, "system", return_value="Linux"),
            patch.object(ports_mod.socket, "gethostbyname", return_value="192.168.65.2"),
        ):
            assert docker_host_address() == "host.docker.internal"

    def test_inside_a_container_with_no_alias_falls_back_to_the_bridge_gateway(self) -> None:
        with (
            patch.object(ports_mod, "running_in_container", return_value=True),
            patch.object(ports_mod.socket, "gethostbyname", side_effect=OSError),
        ):
            assert docker_host_address() == "172.17.0.1"
