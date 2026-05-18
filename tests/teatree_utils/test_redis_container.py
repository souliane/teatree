"""Shared Redis container — docker wrappers, status reporting, slot flushing."""

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.utils import redis_container


def _completed(*, stdout: str = "", returncode: int = 0) -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestDockerLookup:
    def test_raises_when_docker_cli_not_on_path(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(RuntimeError, match="docker CLI not found"):
            redis_container._docker()

    def test_returns_resolved_docker_path(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/docker"):
            assert redis_container._docker() == "/usr/local/bin/docker"


class TestStatus:
    def test_returns_missing_when_inspect_fails(self) -> None:
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(returncode=1)):
            assert redis_container.status() == "missing"

    def test_returns_status_from_docker_inspect(self) -> None:
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(stdout="running\n")):
            assert redis_container.status() == "running"

    def test_returns_missing_when_inspect_empty(self) -> None:
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(stdout="\n")):
            assert redis_container.status() == "missing"


class TestHostPortPublished:
    def test_true_when_6379_mapped_to_host(self) -> None:
        with patch.object(
            redis_container,
            "_docker_tolerant",
            return_value=_completed(stdout="0.0.0.0:6379\n"),
        ):
            assert redis_container._host_port_published() is True

    def test_false_when_no_port_mapping(self) -> None:
        # `docker port teatree-redis 6379` prints nothing when the container
        # was created without `-p 6379:6379` (the "Up 2 days, 6379/tcp:[]"
        # case that silently broke every worktree's web → redis traffic).
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(stdout="\n")):
            assert redis_container._host_port_published() is False

    def test_false_when_docker_port_fails(self) -> None:
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(returncode=1)):
            assert redis_container._host_port_published() is False


class TestEnsureRunning:
    def test_no_op_when_already_running_and_port_published(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_host_port_published", return_value=True),
            patch.object(redis_container, "_docker_checked") as mock,
        ):
            redis_container.ensure_running()
        mock.assert_not_called()

    def test_recreates_running_container_when_port_not_published(self) -> None:
        # Regression: a running teatree-redis created without the host port
        # publish must be reconciled (recreated), not left as-is — otherwise
        # the web container's host.docker.internal:6379 is unreachable and
        # every /api/jwt/ returns HTTP 500 (Error 111). The old code
        # early-returned on status=="running" and never noticed.
        calls: list[tuple[str, ...]] = []
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_host_port_published", return_value=False),
            patch.object(redis_container, "_docker_tolerant", side_effect=lambda *a: calls.append(a) or _completed()),
            patch.object(redis_container, "_docker_checked", side_effect=lambda *a: calls.append(a) or _completed()),
        ):
            redis_container.ensure_running(db_count=16)
        assert ("stop", "teatree-redis") in calls
        assert ("rm", "teatree-redis") in calls
        run_call = next(c for c in calls if c and c[0] == "run")
        assert "-p" in run_call
        assert "6379:6379" in run_call

    def test_creates_container_when_missing(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="missing"),
            patch.object(redis_container, "_docker_checked") as mock,
        ):
            redis_container.ensure_running(db_count=24)
        called_args = mock.call_args.args
        assert called_args[0] == "run"
        assert called_args[1] == "-d"
        assert "teatree-redis" in called_args
        assert "24" in called_args

    def test_starts_existing_stopped_container(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="exited"),
            patch.object(redis_container, "_host_port_published", return_value=True),
            patch.object(redis_container, "_docker_checked") as mock,
        ):
            redis_container.ensure_running()
        mock.assert_called_once_with("start", "teatree-redis")

    def test_recreates_after_start_when_port_not_published(self) -> None:
        # A previously-`docker stop`ped container that was originally created
        # without the publish: `docker start` brings it back still
        # unpublished, so it must also be reconciled.
        calls: list[tuple[str, ...]] = []
        with (
            patch.object(redis_container, "status", return_value="exited"),
            patch.object(redis_container, "_host_port_published", return_value=False),
            patch.object(redis_container, "_docker_tolerant", side_effect=lambda *a: calls.append(a) or _completed()),
            patch.object(redis_container, "_docker_checked", side_effect=lambda *a: calls.append(a) or _completed()),
        ):
            redis_container.ensure_running(db_count=16)
        assert ("start", "teatree-redis") in calls
        assert ("rm", "teatree-redis") in calls
        run_call = next(c for c in calls if c and c[0] == "run")
        assert "6379:6379" in run_call


class TestStop:
    def test_no_op_when_container_missing(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="missing"),
            patch.object(redis_container, "_docker_tolerant") as mock,
        ):
            redis_container.stop()
        mock.assert_not_called()

    def test_stops_when_present(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_docker_tolerant", return_value=_completed()) as mock,
        ):
            redis_container.stop()
        mock.assert_called_once_with("stop", "teatree-redis")


class TestFlushdb:
    def test_raises_when_index_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            redis_container.flushdb(99, db_count=16)

    def test_raises_when_index_negative(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            redis_container.flushdb(-1)

    def test_skips_flush_when_container_not_running(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="missing"),
            patch.object(redis_container, "_docker_tolerant") as mock,
        ):
            redis_container.flushdb(3)
        mock.assert_not_called()

    def test_calls_redis_cli_with_index(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_docker_tolerant", return_value=_completed()) as mock,
        ):
            redis_container.flushdb(3)
        mock.assert_called_once_with("exec", "teatree-redis", "redis-cli", "-n", "3", "FLUSHDB")
