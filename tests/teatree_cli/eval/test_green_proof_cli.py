"""``t3 eval green-proof`` gates on the merged eval-heal JSON (#3202).

Exercised through the real typer CLI so the workflow combine-job invocation is
covered end to end: exit 0 on an executed red-free run, exit 1 on any red or a
missing / empty artifact — the JSON is the enforced proof.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    out = tmp_path / f"eval-heal-{_SHA}.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


def _green_payload() -> dict[str, object]:
    return {
        "head_sha": _SHA,
        "totals": {"total": 2, "passed": 2, "failed": 0, "skipped": 0},
        "scenarios": [
            {"name": "a", "lane": "clean_room", "verdict": "pass", "triage_class": None},
            {"name": "b", "lane": "clean_room", "verdict": "pass", "triage_class": None},
        ],
    }


class TestGreenProofCli:
    def test_green_run_exits_zero(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _green_payload())
        result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 0, result.output
        assert "GREEN PROOF" in result.output

    def test_a_red_run_exits_nonzero(self, tmp_path: Path) -> None:
        payload = _green_payload()
        payload["totals"] = {"total": 2, "passed": 1, "failed": 1, "skipped": 0}
        payload["scenarios"][1]["verdict"] = "fail"  # type: ignore[index]
        payload["scenarios"][1]["triage_class"] = "behavioral"  # type: ignore[index]
        path = _write(tmp_path, payload)
        result = CliRunner().invoke(app, ["eval", "green-proof", str(path)])
        assert result.exit_code == 1, result.output
        assert "NOT A GREEN PROOF" in result.output

    def test_a_missing_artifact_exits_nonzero(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["eval", "green-proof", str(tmp_path / "nope.json")])
        assert result.exit_code == 1, result.output
        assert "no merged eval-heal JSON" in result.output
