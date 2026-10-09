"""A probe that could not answer must say UNKNOWN, never manufacture an answer.

``_probe_open_pr`` classified any non-empty JSON array as FOUND, so a changed forge
output schema yielded ``FOUND`` with an empty url — which the fail-closed teardown
adapter reads as verified ABSENCE.
"""

import json
from pathlib import Path

import pytest

from teatree.core import forge_pr_probe
from teatree.core.forge_pr_probe import PrProbeOutcome, probe_github_open_pr
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.utils.run import CompletedProcess


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestOpenPrProbeRejectsAMalformedRow:
    @pytest.fixture(autouse=True)
    def _routed_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            forge_pr_probe,
            "resolve_repo_token",
            lambda *_args, **_kwargs: ForgeTokenResolution(
                "github_token", "test", ForgeTokenState.TOKEN, token="routed"
            ),
        )

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
