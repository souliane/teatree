"""Stable per-worktree host-port allocation + the shared-network invariant check."""

from teatree.utils.ports import (
    STABLE_PORT_WINDOW_END,
    STABLE_PORT_WINDOW_START,
    SharedNetworkHazard,
    shared_network_hazards,
    stable_host_port,
)


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
