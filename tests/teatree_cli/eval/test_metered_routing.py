"""Docker-by-default routing for the metered eval + benchmark lanes (#2192).

The metered ``sdk`` lane and ``t3 eval benchmark`` default to running IN the CI
container — the reproducible gate must never accidentally bill the host. The
``T3_EVAL_IN_CONTAINER=1`` marker (set by the docker runner) and the explicit
``--local`` escape are the two ways the command runs DIRECTLY in-process; any
other case routes back through docker.
"""

from unittest.mock import patch

import pytest

from teatree.cli.eval.metered_routing import in_container, should_route_to_docker, warn_local_metered

_MODULE = "teatree.cli.eval.metered_routing"


class TestInContainer:
    def test_true_when_marker_set(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"T3_EVAL_IN_CONTAINER": "1"}):
            assert in_container() is True

    def test_false_when_marker_absent(self) -> None:
        with patch(f"{_MODULE}.os.environ", {}):
            assert in_container() is False

    def test_false_when_marker_empty(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"T3_EVAL_IN_CONTAINER": ""}):
            assert in_container() is False


class TestShouldRouteToDocker:
    def test_metered_routes_to_docker_by_default(self) -> None:
        with patch(f"{_MODULE}.os.environ", {}):
            assert should_route_to_docker(metered=True, local=False) is True

    def test_local_escape_runs_in_process(self) -> None:
        with patch(f"{_MODULE}.os.environ", {}):
            assert should_route_to_docker(metered=True, local=True) is False

    def test_in_container_runs_in_process(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"T3_EVAL_IN_CONTAINER": "1"}):
            assert should_route_to_docker(metered=True, local=False) is False

    def test_non_metered_lane_stays_host_default(self) -> None:
        # Free / deterministic / subscription lanes never spawn an agent, so they
        # are not subject to docker-by-default.
        with patch(f"{_MODULE}.os.environ", {}):
            assert should_route_to_docker(metered=False, local=False) is False


class TestLocalMeteredWarning:
    def test_warns_for_sdk_backend(self, capsys: pytest.CaptureFixture[str]) -> None:
        warn_local_metered(metered=True)
        err = capsys.readouterr().err
        assert "WARNING" in err
        lowered = err.lower()
        assert any(token in lowered for token in ("reproducible", "docker", "ci"))

    def test_silent_for_non_metered(self, capsys: pytest.CaptureFixture[str]) -> None:
        warn_local_metered(metered=False)
        assert capsys.readouterr().err == ""
