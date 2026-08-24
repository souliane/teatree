"""Tests for cli/dependency_audit_tools.py — the ``t3 tool dependency-audit`` surface (#4346)."""

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.dependency_audit_tools import register, run_dependency_audit
from tests._color_env import strip_ansi

runner = CliRunner()

_REPORT = {
    "dependencies": [
        {
            "name": "django",
            "version": "6.0.7",
            "vulns": [
                {
                    "id": "PYSEC-2026-1",
                    "aliases": ["CVE-2026-15307"],
                    "description": "Spatial lookups passing str to django.contrib.gis.gdal.GDALRaster.",
                }
            ],
        }
    ]
}


def _write_report(tmp_path: Path, body: object = _REPORT) -> Path:
    report = tmp_path / "audit-report.json"
    report.write_text(json.dumps(body), encoding="utf-8")
    return report


def _write_src(tmp_path: Path, body: str) -> None:
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "mod.py").write_text(body, encoding="utf-8")


class TestRegister:
    def test_registers_the_command_onto_a_fresh_app(self) -> None:
        fresh_app = typer.Typer()
        register(fresh_app)
        result = runner.invoke(fresh_app, ["dependency-audit", "--help"])
        assert result.exit_code == 0
        assert "--report" in strip_ansi(result.output)

    def test_the_shared_tool_app_carries_the_registration(self) -> None:
        # `teatree.cli` builds `app` by calling `.register(tool_app)` for every
        # module in `_TOOL_MODULES`, this one included — a missing or broken
        # registration there would surface as "No such command" in every other
        # test in this file, so this test names the property directly.
        result = runner.invoke(app, ["tool", "dependency-audit", "--help"])
        assert result.exit_code == 0
        assert "--report" in strip_ansi(result.output)


class TestRunDependencyAudit:
    def test_reads_a_report_file_and_prints_the_formatted_assessment(self, tmp_path: Path) -> None:
        _write_src(tmp_path, "import django.db.models\n")
        report = _write_report(tmp_path)
        result = runner.invoke(app, ["tool", "dependency-audit", "--report", str(report), "--root", str(tmp_path)])
        assert result.exit_code == 0
        assert "django 6.0.7 — PYSEC-2026-1 (CVE-2026-15307)" in result.output
        assert "REACHABLE from src/" in result.output
        assert "django.contrib.gis.gdal.GDALRaster — NOT reachable from src/" in result.output

    def test_reads_from_stdin_when_report_is_a_dash(self, tmp_path: Path) -> None:
        _write_src(tmp_path, "import django.db.models\n")
        result = runner.invoke(
            app,
            ["tool", "dependency-audit", "--report", "-", "--root", str(tmp_path)],
            input=json.dumps(_REPORT),
        )
        assert result.exit_code == 0
        assert "REACHABLE from src/" in result.output

    def test_json_flag_emits_machine_readable_output(self, tmp_path: Path) -> None:
        _write_src(tmp_path, "import django.db.models\n")
        report = _write_report(tmp_path)
        result = runner.invoke(
            app, ["tool", "dependency-audit", "--report", str(report), "--root", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0
        (entry,) = json.loads(result.output)
        assert entry["package"] == "django"
        assert entry["package_reach"] == "imported"
        assert entry["components"][0]["reach"] == "not_imported"

    def test_defaults_root_to_the_current_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Calling the typer command function directly (not via CliRunner) exercises
        # the `root or Path.cwd()` branch with root=None explicitly.
        _write_src(tmp_path, "import django.db.models\n")
        report = _write_report(tmp_path, {"dependencies": []})
        monkeypatch.chdir(tmp_path)
        run_dependency_audit(report=str(report), root=None, output_json=False)

    def test_an_unreadable_report_exits_one_and_never_prints_a_finding_list(self, tmp_path: Path) -> None:
        _write_src(tmp_path, "import django.db.models\n")
        report = tmp_path / "broken.json"
        report.write_text("not json", encoding="utf-8")
        result = runner.invoke(app, ["tool", "dependency-audit", "--report", str(report), "--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "could not read the audit report" in result.output

    def test_a_missing_report_file_exits_one(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["tool", "dependency-audit", "--report", str(tmp_path / "missing.json"), "--root", str(tmp_path)],
        )
        assert result.exit_code == 1

    def test_an_empty_report_says_no_advisories(self, tmp_path: Path) -> None:
        _write_src(tmp_path, "import django.db.models\n")
        report = _write_report(tmp_path, {"dependencies": []})
        result = runner.invoke(app, ["tool", "dependency-audit", "--report", str(report), "--root", str(tmp_path)])
        assert result.exit_code == 0
        assert "no advisories to assess" in result.output
