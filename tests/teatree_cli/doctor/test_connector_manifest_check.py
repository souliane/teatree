"""`t3 doctor check` — the per-overlay connector-manifest gate (PR-19 item 3).

Drives ``_check_connector_manifest`` against injected outcomes so the FAIL /
WARN / RECONNECT-line behaviour is asserted without a ``claude mcp list``
subprocess.
"""

from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

from teatree.cli.doctor.checks import _check_connector_manifest
from teatree.core.connector_manifest import ConnectorManifestOutcome, ConnectorRequirement, DownConnector


def _patched(outcome: ConnectorManifestOutcome) -> AbstractContextManager[MagicMock]:
    return patch("teatree.core.connector_manifest.check_connector_manifest", return_value=outcome)


class TestCheckConnectorManifest:
    def test_all_connected_passes(self) -> None:
        with _patched(ConnectorManifestOutcome(ok=True)):
            assert _check_connector_manifest() is True

    def test_required_down_fails_with_reconnect_line(self, capsys) -> None:
        down = [DownConnector(requirement=ConnectorRequirement("claude.ai Slack"), overlay="ov", ever_connected=True)]
        outcome = ConnectorManifestOutcome(
            ok=False,
            down=down,
            required_findings=["connector 'claude.ai Slack' (required by overlay 'ov') is down — reconnect it ..."],
        )
        with _patched(outcome):
            assert _check_connector_manifest() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "RECONNECT claude.ai Slack -> https://claude.ai/settings/connectors" in out

    def test_optional_down_warns_but_passes(self, capsys) -> None:
        req = ConnectorRequirement("x", required=False)
        down = [DownConnector(requirement=req, overlay="ov", ever_connected=False)]
        outcome = ConnectorManifestOutcome(
            ok=True,
            down=down,
            optional_findings=["optional connector 'x' (overlay 'ov') is not connected — ..."],
        )
        with _patched(outcome):
            assert _check_connector_manifest() is True
        assert "WARN" in capsys.readouterr().out

    def test_degraded_probe_warns_and_passes(self, capsys) -> None:
        outcome = ConnectorManifestOutcome(
            ok=True,
            degraded=True,
            probe_findings=["Could not live-probe (claude absent)"],
        )
        with _patched(outcome):
            assert _check_connector_manifest() is True
        assert "WARN" in capsys.readouterr().out

    def test_crash_degrades_to_warn(self, capsys) -> None:
        with patch("teatree.core.connector_manifest.check_connector_manifest", side_effect=RuntimeError("boom")):
            assert _check_connector_manifest() is True
        assert "crashed" in capsys.readouterr().out
