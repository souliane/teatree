from pathlib import Path

import pytest
import typer

from teatree.cli.eval.metered_trials import park_on_usage_limit


def test_usage_limit_writes_a_parked_artifact(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    with pytest.raises(typer.Exit) as exc:
        park_on_usage_limit(summary_md=summary)
    assert exc.value.exit_code == 75
    assert "PARKED" in summary.read_text()


def test_prior_failure_keeps_the_red_verdict_and_dashboard(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard.html"
    with pytest.raises(typer.Exit) as exc:
        park_on_usage_limit(html_out=dashboard, failed_before_limit=True)
    assert exc.value.exit_code == 1
    assert "failed before the usage limit" in dashboard.read_text()
