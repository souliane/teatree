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
            patch.object(redis_container, "_configured_db_count", return_value=redis_container.DEFAULT_DB_COUNT),
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
            patch.object(redis_container, "_docker_tolerant", return_value=_completed()),
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
            patch.object(redis_container, "_configured_db_count", return_value=redis_container.DEFAULT_DB_COUNT),
            patch.object(redis_container, "_docker_tolerant", return_value=_completed()),
            patch.object(redis_container, "_docker_checked") as mock,
        ):
            redis_container.ensure_running()
        mock.assert_called_once_with("start", "teatree-redis")

    def test_evicts_non_teatree_squatter_holding_6379_before_create(self) -> None:
        # Regression (#1373): a legacy overlay-managed container squats on
        # host port 6379 (a docker-compose.yml predating `profiles: [disabled]`
        # left a redis service publishing the port). The canonical
        # teatree-redis is missing, so `_create()` would
        # `bind: address already in use` and silently leave teatree-redis in
        # Created state. Reconciliation must stop+remove the squatter first,
        # then create teatree-redis.
        calls: list[tuple[str, ...]] = []

        def fake_tolerant(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ("ps", "--filter"):
                return _completed(stdout="legacy-redis-squatter\n")
            return _completed()

        def fake_checked(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            return _completed()

        with (
            patch.object(redis_container, "status", return_value="missing"),
            patch.object(redis_container, "_docker_tolerant", side_effect=fake_tolerant),
            patch.object(redis_container, "_docker_checked", side_effect=fake_checked),
        ):
            redis_container.ensure_running(db_count=16)

        assert ("stop", "legacy-redis-squatter") in calls
        assert ("rm", "legacy-redis-squatter") in calls
        run_call = next(c for c in calls if c and c[0] == "run")
        assert "teatree-redis" in run_call
        assert "6379:6379" in run_call

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

    def test_recreates_running_container_with_too_few_databases(self) -> None:
        # Redis bakes `--databases N` in at boot; a running container started
        # with `--databases 16` never picks up a raised `redis_db_count`, so
        # slot allocation keeps failing once 16 are held. Reconciliation must
        # recreate it with the larger pool. Anti-vacuity: drop the db-count
        # branch from `_needs_recreate` and this goes RED (no recreate fires).
        calls: list[tuple[str, ...]] = []
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_host_port_published", return_value=True),
            patch.object(redis_container, "_configured_db_count", return_value=16),
            patch.object(redis_container, "_docker_tolerant", side_effect=lambda *a: calls.append(a) or _completed()),
            patch.object(redis_container, "_docker_checked", side_effect=lambda *a: calls.append(a) or _completed()),
        ):
            redis_container.ensure_running(db_count=64)
        assert ("stop", "teatree-redis") in calls
        assert ("rm", "teatree-redis") in calls
        run_call = next(c for c in calls if c and c[0] == "run")
        assert "--databases" in run_call
        assert "64" in run_call

    def test_no_recreate_when_running_container_has_enough_databases(self) -> None:
        # A container already started with the requested (or a larger) pool is
        # left untouched — no churn on the shared container.
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_host_port_published", return_value=True),
            patch.object(redis_container, "_configured_db_count", return_value=64),
            patch.object(redis_container, "_docker_checked") as mock,
        ):
            redis_container.ensure_running(db_count=64)
        mock.assert_not_called()

    def test_no_recreate_when_db_count_probe_inconclusive(self) -> None:
        # An inconclusive `CONFIG GET databases` probe (docker hiccup) must NOT
        # churn the shared container — fail-safe to "leave it running".
        with (
            patch.object(redis_container, "status", return_value="running"),
            patch.object(redis_container, "_host_port_published", return_value=True),
            patch.object(redis_container, "_configured_db_count", return_value=None),
            patch.object(redis_container, "_docker_checked") as mock,
        ):
            redis_container.ensure_running(db_count=64)
        mock.assert_not_called()


class TestConfiguredDbCount:
    def test_parses_databases_value_from_config_get(self) -> None:
        with patch.object(
            redis_container,
            "_docker_tolerant",
            return_value=_completed(stdout="databases\n64\n"),
        ):
            assert redis_container._configured_db_count() == 64

    def test_returns_none_when_exec_fails(self) -> None:
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(returncode=1)):
            assert redis_container._configured_db_count() is None

    def test_returns_none_when_value_unparseable(self) -> None:
        with patch.object(
            redis_container,
            "_docker_tolerant",
            return_value=_completed(stdout="databases\nnot-a-number\n"),
        ):
            assert redis_container._configured_db_count() is None

    def test_returns_none_when_reply_truncated(self) -> None:
        with patch.object(redis_container, "_docker_tolerant", return_value=_completed(stdout="databases\n")):
            assert redis_container._configured_db_count() is None


class TestNativeSquatterDetection:
    # lsof -F pc output: each field is prefixed with its type char, so a
    # `redis-server` command appears as the line `credis-server` and a `p<pid>`
    # line precedes it.
    def test_reports_native_redis_listener(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/lsof"),
            patch.object(
                redis_container,
                "run_allowed_to_fail",
                return_value=_completed(stdout="p4242\ncredis-server\n"),
            ),
        ):
            assert redis_container._native_host_listeners() == ["4242/redis-server"]

    def test_ignores_docker_port_forwarder(self) -> None:
        # Docker's own publish proxy holds the port on behalf of teatree-redis —
        # that is not a native squatter and must not be reported. The lsof
        # command field for `com.docker.backend` prints as `ccom.docker.backend`.
        with (
            patch("shutil.which", return_value="/usr/bin/lsof"),
            patch.object(
                redis_container,
                "run_allowed_to_fail",
                return_value=_completed(stdout="p99\nccom.docker.backend\n"),
            ),
        ):
            assert redis_container._native_host_listeners() == []

    def test_returns_empty_when_lsof_absent(self) -> None:
        with patch("shutil.which", return_value=None):
            assert redis_container._native_host_listeners() == []


class TestNativeSquatterGuard:
    def test_ensure_running_raises_when_native_redis_holds_port(self) -> None:
        # Regression (#1373 sibling): a native `redis-server` binds host 6379,
        # so `_create()` would fail with `bind: address already in use` and
        # leave teatree-redis in Created state. _evict_squatters only handles
        # container squatters, so provision must fail loud naming the process.
        with (
            patch.object(redis_container, "status", return_value="missing"),
            patch.object(redis_container, "_non_teatree_squatters_on_host_port", return_value=[]),
            patch.object(redis_container, "_native_host_listeners", return_value=["4242/redis-server"]),
            patch.object(redis_container, "_docker_checked") as mock_create,
            pytest.raises(redis_container.NativeRedisSquatterError, match="4242/redis-server"),
        ):
            redis_container.ensure_running(db_count=16)
        mock_create.assert_not_called()

    def test_ensure_running_creates_when_no_native_listener(self) -> None:
        with (
            patch.object(redis_container, "status", return_value="missing"),
            patch.object(redis_container, "_non_teatree_squatters_on_host_port", return_value=[]),
            patch.object(redis_container, "_native_host_listeners", return_value=[]),
            patch.object(redis_container, "_docker_tolerant", return_value=_completed()),
            patch.object(redis_container, "_docker_checked", return_value=_completed()) as mock_create,
        ):
            redis_container.ensure_running(db_count=16)
        assert mock_create.call_args.args[0] == "run"


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
