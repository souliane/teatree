"""Disk contributor probes are bounded and never hide the root alarm."""

from unittest.mock import patch

from teatree.utils import disk_consumers


def test_docker_sizes_parse_without_shelling_out() -> None:
    assert disk_consumers._bytes_from_docker_size("1.5GB") == 1_500_000_000
    assert disk_consumers._bytes_from_docker_size("garbage") is None


def test_summary_orders_measured_consumers_and_keeps_unknown_visible() -> None:
    with (
        patch.object(disk_consumers, "_docker_build_cache_bytes", return_value=12 * 1024**3),
        patch.object(disk_consumers, "_worktree_bytes", return_value=7 * 1024**3),
        patch.object(disk_consumers, "_control_db_bytes", return_value=None),
    ):
        summary = disk_consumers.summary()
    assert summary.startswith("Docker build cache 12.0 GiB; worktrees/env dirs 7.0 GiB")
    assert "control DB unknown" in summary


def test_a_failed_contributor_does_not_hide_the_other_sizes() -> None:
    with (
        patch.object(disk_consumers, "_docker_build_cache_bytes", side_effect=OSError("daemon wedged")),
        patch.object(disk_consumers, "_worktree_bytes", return_value=1024**3),
        patch.object(disk_consumers, "_control_db_bytes", return_value=0),
    ):
        summary = disk_consumers.summary()
    assert "Docker build cache unknown" in summary
    assert "worktrees/env dirs 1.0 GiB" in summary
