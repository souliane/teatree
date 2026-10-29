"""The eval page command renders the durable ledger, including an output file."""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.models import EvalRunRecord, EvalVerdict


class TestPage(TestCase):
    def test_page_reports_no_recorded_runs(self) -> None:
        result = CliRunner().invoke(app, ["eval", "page"])

        assert result.exit_code == 0, result.output
        assert "no recorded runs" in result.output

    def test_page_writes_current_recorded_run(self) -> None:
        run = EvalRunRecord.objects.create(model="subscription")
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.FAIL, cost_usd=0.25)
        with TemporaryDirectory() as directory:
            out = Path(directory) / "results.md"
            result = CliRunner().invoke(app, ["eval", "page", "--out", str(out)])

            assert result.exit_code == 0, result.output
            assert out.read_text().startswith(f"# Behavioral eval results — {run.started_at.date().isoformat()}")
            assert "alpha" in out.read_text()
