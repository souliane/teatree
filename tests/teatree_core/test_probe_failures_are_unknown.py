"""A probe that could not answer must say UNKNOWN, never manufacture an answer.

Two external readers that turned a FAILED read into a confident value:

*   ``probe_mcp_servers`` accepted any ``claude mcp list`` exit code, so an auth
    error (exit 1, empty stdout) parsed as an empty status list and every enabled
    server was reported as PROVEN disconnected instead of degrading to the
    documented unverifiable WARN.
*   ``_probe_open_pr`` classified any non-empty JSON array as FOUND, so a changed
    forge output schema yielded ``FOUND`` with an empty url — which the
    fail-closed teardown adapter reads as verified ABSENCE.
"""

import json
from pathlib import Path

import pytest

from teatree.core import forge_pr_probe, mcp_connectivity
from teatree.core.forge_pr_probe import PrProbeOutcome, probe_github_open_pr
from teatree.core.mcp_connectivity import ConfiguredMcpServer, check_mcp_connectivity, probe_mcp_servers
from teatree.utils import run as run_module
from teatree.utils.run import CommandFailedError, CompletedProcess


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestMcpProbeRaisesOnASubprocessFailure:
    def test_non_zero_exit_raises_rather_than_reporting_zero_servers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real ``run_allowed_to_fail`` decides on ``expected_codes``, so the
        # subprocess itself is the only thing faked.
        monkeypatch.setattr(mcp_connectivity.shutil, "which", lambda _name: str(tmp_path / "claude"))
        monkeypatch.setattr(
            run_module.subprocess,
            "run",
            lambda *_a, **_k: _completed(1, stdout="", stderr="not authenticated"),
        )
        with pytest.raises(CommandFailedError):
            probe_mcp_servers()

    def test_a_clean_exit_still_parses(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(mcp_connectivity.shutil, "which", lambda _name: str(tmp_path / "claude"))
        monkeypatch.setattr(
            run_module.subprocess,
            "run",
            lambda *_a, **_k: _completed(0, stdout="slack: https://mcp.slack.com - ✔ Connected\n"),
        )
        statuses = probe_mcp_servers()
        assert [(s.name, s.connected) for s in statuses] == [("slack", True)]

    def test_check_degrades_to_a_warning_instead_of_claiming_disconnection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> list[object]:
            raise CommandFailedError(["claude", "mcp", "list"], 1, "", "not authenticated")

        monkeypatch.setattr(
            mcp_connectivity,
            "read_enabled_mcp_servers",
            lambda **_k: [ConfiguredMcpServer(name="slack", provider=mcp_connectivity.THIRD_PARTY)],
        )
        outcome = check_mcp_connectivity(probe=_raise)

        assert outcome.degraded is True
        assert not any("NOT connected" in finding for finding in outcome.findings)


class TestOpenPrProbeRejectsAMalformedRow:
    def test_row_without_the_url_key_is_unknown(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            forge_pr_probe,
            "run_allowed_to_fail",
            lambda *_a, **_k: _completed(0, stdout=json.dumps([{"id": 17}])),
        )
        assert probe_github_open_pr(tmp_path, "feat-x").outcome is PrProbeOutcome.UNKNOWN

    def test_row_with_a_blank_url_is_unknown(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            forge_pr_probe,
            "run_allowed_to_fail",
            lambda *_a, **_k: _completed(0, stdout=json.dumps([{"url": ""}])),
        )
        assert probe_github_open_pr(tmp_path, "feat-x").outcome is PrProbeOutcome.UNKNOWN

    def test_well_formed_row_is_still_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        url = "https://github.com/acme/widgets/pull/7"
        monkeypatch.setattr(
            forge_pr_probe,
            "run_allowed_to_fail",
            lambda *_a, **_k: _completed(0, stdout=json.dumps([{"url": url}])),
        )
        probe = probe_github_open_pr(tmp_path, "feat-x")
        assert probe.outcome is PrProbeOutcome.FOUND
        assert probe.url == url

    def test_empty_array_is_still_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(forge_pr_probe, "run_allowed_to_fail", lambda *_a, **_k: _completed(0, stdout="[]"))
        assert probe_github_open_pr(tmp_path, "feat-x").outcome is PrProbeOutcome.NONE
