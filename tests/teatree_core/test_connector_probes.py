"""The reusable connector-preflight probe library + transient taxonomy (#3333).

Anti-vacuity: the classifier is proven against a RAISED connect-timeout and a
RAISED 5xx returning transient, AND a 4xx returning definitive — a test that only
asserted "probe passes when the service is up" would pass against an empty probe
list.
"""

from unittest.mock import patch

import httpx
import pytest

from teatree.core.connector_manifest import ConnectorRequirement, ConnectorUnavailableError
from teatree.core.connector_probes import is_transient, reachability_probe, standard_probes


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test")
    return httpx.HTTPStatusError(f"{code}", request=request, response=httpx.Response(code, request=request))


class TestIsTransient:
    def test_connect_timeout_is_transient(self) -> None:
        assert is_transient(httpx.ConnectTimeout("timed out")) is True

    def test_dns_connect_error_is_transient(self) -> None:
        assert is_transient(httpx.ConnectError("name resolution failed")) is True

    def test_5xx_is_transient(self) -> None:
        assert is_transient(_status_error(503)) is True

    def test_4xx_is_definitive(self) -> None:
        assert is_transient(_status_error(403)) is False

    def test_unsupported_scheme_is_definitive(self) -> None:
        assert is_transient(httpx.UnsupportedProtocol("no scheme")) is False

    def test_unrecognised_error_is_definitive(self) -> None:
        assert is_transient(ValueError("boom")) is False


class TestReachabilityProbe:
    def test_any_http_status_proves_the_host_is_up(self) -> None:
        # A 403 is a SUCCESS signal for reachability — the host answered.
        with patch("teatree.core.connector_probes.httpx.get", return_value=httpx.Response(403)):
            reachability_probe(name="api", url="https://api.test", required=True)()  # no raise

    def test_transient_transport_failure_never_hard_fails(self) -> None:
        with patch("teatree.core.connector_probes.httpx.get", side_effect=httpx.ConnectTimeout("blip")):
            reachability_probe(name="api", url="https://api.test", required=True)()  # fail-open

    def test_definitive_failure_raises_for_a_required_connector(self) -> None:
        with (
            patch("teatree.core.connector_probes.httpx.get", side_effect=httpx.UnsupportedProtocol("no scheme")),
            pytest.raises(RuntimeError, match="unreachable"),
        ):
            reachability_probe(name="api", url="api.test", required=True)()

    def test_definitive_failure_only_warns_for_an_optional_connector(self) -> None:
        with patch("teatree.core.connector_probes.httpx.get", side_effect=httpx.UnsupportedProtocol("no scheme")):
            reachability_probe(name="api", url="api.test", required=False)()  # no raise


class TestStandardProbes:
    def test_no_expectations_yields_no_probes(self) -> None:
        assert standard_probes([ConnectorRequirement(name="claude.ai Slack")], expectations={}) == []

    def test_required_server_probe_raises_when_down(self) -> None:
        manifest = [ConnectorRequirement(name="slack", required=True)]
        probes = standard_probes(manifest, expectations={"slack": "slack"})
        assert len(probes) == 1
        with (
            patch(
                "teatree.core.connector_probes.require_connector",
                side_effect=ConnectorUnavailableError("slack"),
            ),
            pytest.raises(RuntimeError, match="slack"),
        ):
            probes[0]()

    def test_optional_server_probe_warns_when_down(self) -> None:
        manifest = [ConnectorRequirement(name="notion", required=False)]
        probes = standard_probes(manifest, expectations={"notion": "notion"})
        with patch(
            "teatree.core.connector_probes.require_connector",
            side_effect=ConnectorUnavailableError("notion"),
        ):
            probes[0]()  # optional — warns, never raises

    def test_connected_server_probe_passes(self) -> None:
        manifest = [ConnectorRequirement(name="slack", required=True)]
        probes = standard_probes(manifest, expectations={"slack": "slack"})
        with patch("teatree.core.connector_probes.require_connector", return_value=None):
            probes[0]()  # connected — no raise
