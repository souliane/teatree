"""``t3 eval green-proof`` gates on the merged eval-heal JSON (#3202).

Exercised through the real typer CLI so the workflow combine-job invocation is
covered end to end: exit 0 on an executed red-free run, exit 1 on any red or a
missing / empty artifact — the JSON is the enforced proof.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.discovery import CORE_CATALOG_FLOOR, ScenarioCatalog

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    out = tmp_path / f"eval-heal-{_SHA}.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


def _green_payload() -> dict[str, Any]:
    return {
        "head_sha": _SHA,
        "totals": {"total": 2, "passed": 2, "failed": 0, "skipped": 0},
        "scenarios": [
            {"name": "a", "lane": "clean_room", "verdict": "pass", "triage_class": None},
            {"name": "b", "lane": "clean_room", "verdict": "pass", "triage_class": None},
        ],
    }


def _catalog_of(size: int, *, degraded: dict[str, str] | None = None):
    """Pin the expected scenario count the CLI derives from the live catalog."""
    catalog = ScenarioCatalog(specs=[object()] * size, degraded=degraded or {}, core_count=CORE_CATALOG_FLOOR)
    return patch("teatree.cli.eval.green_proof.discover_catalog", return_value=catalog)


class TestGreenProofCli:
    def test_green_run_exits_zero(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _green_payload())
        with _catalog_of(2):
            result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 0, result.output
        assert "GREEN PROOF" in result.output

    def test_a_run_covering_less_than_the_catalog_exits_nonzero(self, tmp_path: Path) -> None:
        # Seven of eight shards uploaded nothing; the survivor is all-green and
        # proves nothing about the scenarios it never carried.
        path = _write(tmp_path, _green_payload())
        with _catalog_of(231):
            result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 1, result.output
        assert "NOT A GREEN PROOF" in result.output

    def test_a_red_run_exits_nonzero(self, tmp_path: Path) -> None:
        payload = _green_payload()
        payload["totals"] = {"total": 2, "passed": 1, "failed": 1, "skipped": 0}
        payload["scenarios"][1]["verdict"] = "fail"
        payload["scenarios"][1]["triage_class"] = "behavioral"
        path = _write(tmp_path, payload)
        with _catalog_of(2):
            result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 1, result.output
        assert "NOT A GREEN PROOF" in result.output

    def test_a_missing_artifact_exits_nonzero(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["eval", "green-proof", str(tmp_path / "nope.json")])
        assert result.exit_code == 1, result.output
        assert "no merged eval-heal JSON" in result.output

    def test_a_degraded_catalog_refuses_the_proof_it_would_otherwise_satisfy(self, tmp_path: Path) -> None:
        # The self-defeating shape: the raising overlay shrinks the catalog AND the
        # expected count with it, so the run "covers" a denominator derived from the
        # same incomplete read. Coverage must be refused, not recomputed.
        path = _write(tmp_path, _green_payload())
        with _catalog_of(2, degraded={"acme": "boom"}):
            result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 1, result.output
        assert "NOT A GREEN PROOF" in result.output
        assert "acme: boom" in result.output

    def test_an_overlay_naming_a_missing_dir_refuses_the_proof(self, tmp_path: Path) -> None:
        # The sibling route, through the LIVE catalog rather than a stub: the hook
        # succeeded, so nothing raised, and the denominator shrank anyway.
        path = _write(tmp_path, _green_payload())
        overlay = SimpleNamespace(get_eval_scenarios_dir=lambda: tmp_path / "moved-away")
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-moved": overlay}):
            result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 1, result.output
        assert "DEGRADED" in result.output
        assert "t3-moved" in result.output

    def test_a_whole_registry_failure_refuses_the_proof(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _green_payload())
        with _catalog_of(2, degraded={"*": "entry points unreadable"}):
            result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 1, result.output
        assert "*: entry points unreadable" in result.output
