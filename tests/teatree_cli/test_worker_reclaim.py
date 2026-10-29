"""The deploy's worker reclaim command is reachable through the public CLI."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.worker import worker_app
from teatree.cli.worker_reclaim import run_reclaim


def test_worker_exposes_reclaim_command() -> None:
    result = CliRunner().invoke(worker_app, ["--help"])

    assert result.exit_code == 0
    assert "reclaim" in result.stdout


def test_worker_reclaim_routes_budget_through_cli() -> None:
    with patch("teatree.cli.worker_reclaim.run_reclaim", return_value=("disk pass", "artifact pass")) as run:
        result = CliRunner().invoke(worker_app, ["reclaim", "--budget-seconds", "90"])

    assert result.exit_code == 0
    assert "disk pass" in result.stdout
    assert "artifact pass" in result.stdout
    run.assert_called_once_with(budget_seconds=90)


def test_worker_reclaim_passes_shared_pressure_and_remaining_budget() -> None:
    settings = SimpleNamespace(
        disk_warn_free_gb=25.0,
        disk_crit_free_gb=10.0,
        disk_cache_allowlist=["/safe/cache"],
        artifact_idle_days=2.0,
    )
    reading = SimpleNamespace(disk_free_gb=16.0, disk_probe_degraded=False)
    marker = SimpleNamespace(last_plan="disk pass", last_artifact_plan="artifact pass")
    with (
        patch("teatree.cli.worker_reclaim.ensure_django"),
        patch("teatree.config.get_effective_settings", return_value=settings),
        patch("teatree.loop.scanners.resource_pressure.measure_resources", return_value=reading),
        patch("teatree.loop.mechanical_resources.free_resources") as disk,
        patch("teatree.loop.mechanical_artifacts.sweep_artifacts") as artifacts,
        patch("teatree.core.models.resource_pressure_marker.ResourcePressureMarker.load", return_value=marker),
        patch("teatree.cli.worker_reclaim.time.monotonic", side_effect=[0.0, 30.0]),
    ):
        result = run_reclaim(budget_seconds=90)

    assert result == ("disk pass", "artifact pass")
    assert disk.call_args.args[0]["budget_seconds"] == 45
    assert disk.call_args.args[0]["disk_cache_allowlist"] == ["/safe/cache"]
    assert artifacts.call_args.args[0]["budget_seconds"] == 60
    assert artifacts.call_args.args[0]["free_gb"] == pytest.approx(16.0)
