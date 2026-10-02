"""Stable per-worktree host-port allocation, the container boundary, and the shared-network check."""

from unittest.mock import patch

import pytest

from teatree.utils import ports as ports_mod
from teatree.utils.ports import (
    STABLE_PORT_WINDOW_END,
    STABLE_PORT_WINDOW_START,
    SharedNetworkHazard,
    docker_host_address,
    shared_network_hazards,
    stable_host_port,
)


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


class TestStableHostPort:
    """A deterministic port in the window, stable across calls, probing on conflict."""

    def _always_free(self, _port: int) -> bool:
        return True

    def test_is_deterministic_across_calls(self) -> None:
        first = stable_host_port("wt/acme/123", 8000, is_available=self._always_free)
        second = stable_host_port("wt/acme/123", 8000, is_available=self._always_free)
        assert first == second

    def test_stays_within_the_window(self) -> None:
        for identity in ("a", "b", "c", "wt/x/9", "another-worktree"):
            port = stable_host_port(identity, 8000, is_available=self._always_free)
            assert STABLE_PORT_WINDOW_START <= port <= STABLE_PORT_WINDOW_END

    def test_window_sits_below_the_default_ephemeral_floor(self) -> None:
        # Linux default net.ipv4.ip_local_port_range starts at 32768; the stable
        # window must end below it so an assignment never collides with a
        # kernel-handed ephemeral port.
        assert STABLE_PORT_WINDOW_END < 32768

    def test_different_container_ports_differ(self) -> None:
        backend = stable_host_port("wt/acme/1", 8000, is_available=self._always_free)
        frontend = stable_host_port("wt/acme/1", 80, is_available=self._always_free)
        assert backend != frontend

    def test_probes_forward_on_conflict(self) -> None:
        taken = stable_host_port("wt/acme/1", 8000, is_available=self._always_free)
        result = stable_host_port("wt/acme/1", 8000, is_available=lambda p: p != taken)
        assert result != taken
        assert STABLE_PORT_WINDOW_START <= result <= STABLE_PORT_WINDOW_END

    def test_falls_back_to_base_when_window_exhausted(self) -> None:
        base = stable_host_port("wt/acme/1", 8000, is_available=self._always_free)
        exhausted = stable_host_port("wt/acme/1", 8000, is_available=lambda _p: False)
        assert exhausted == base


class TestSharedNetworkHazards:
    """Flag a service attached to a network shared across worktree projects."""

    def test_flags_service_on_external_network(self) -> None:
        compose = {
            "services": {"web": {"networks": ["shared"]}},
            "networks": {"shared": {"external": True}},
        }
        hazards = shared_network_hazards(compose)
        assert hazards == [SharedNetworkHazard(service="web", network="shared")]

    def test_flags_network_pinned_to_fixed_name(self) -> None:
        compose = {
            "services": {"web": {"networks": {"shared": None}}},
            "networks": {"shared": {"name": "global_net"}},
        }
        hazards = shared_network_hazards(compose)
        assert [h.service for h in hazards] == ["web"]

    def test_message_names_service_and_hazard(self) -> None:
        hazard = SharedNetworkHazard(service="frontend", network="shared")
        message = hazard.format()
        assert "frontend" in message
        assert "shared" in message
        assert "across worktree" in message.lower()

    def test_project_scoped_network_is_not_flagged(self) -> None:
        compose = {
            "services": {"web": {"networks": ["default"]}},
            "networks": {"default": {}},
        }
        assert shared_network_hazards(compose) == []

    def test_service_not_on_shared_network_is_not_flagged(self) -> None:
        compose = {
            "services": {"web": {"networks": ["private"]}, "db": {"networks": ["shared"]}},
            "networks": {"shared": {"external": True}, "private": {}},
        }
        hazards = shared_network_hazards(compose)
        assert [h.service for h in hazards] == ["db"]

    def test_no_networks_section_is_safe(self) -> None:
        assert shared_network_hazards({"services": {"web": {}}}) == []
