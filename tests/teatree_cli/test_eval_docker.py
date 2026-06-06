"""``t3 eval all --docker`` — CI-image-parity local run."""

from unittest.mock import MagicMock, patch

import pytest

from teatree.cli.eval.docker import DOCKER_IMAGE, DockerUnavailableError, _image_present, run_eval_in_docker

_MODULE = "teatree.cli.eval.docker"


def _completed(returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    return proc


class TestRunEvalInDocker:
    def test_builds_image_when_absent_then_runs(self) -> None:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=False),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            code = run_eval_in_docker(["all", "--free-only"])
        assert code == 0
        build, exec_ = streamed.call_args_list
        assert "build" in build.args[0]
        assert "-f" in build.args[0]
        assert "dev/Dockerfile.test" in build.args[0]
        assert exec_.args[0][:2] == ["docker", "run"]

    def test_skips_build_when_image_present(self) -> None:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["all", "--free-only"])
        assert streamed.call_count == 1
        assert streamed.call_args.args[0][:2] == ["docker", "run"]

    def test_passes_eval_args_through_to_container(self) -> None:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["all", "--free-only"])
        container_cmd = streamed.call_args.args[0]
        start = container_cmd.index("uv")
        assert container_cmd[start:] == ["uv", "run", "t3", "eval", "all", "--free-only"]

    def test_propagates_nonzero_exit_from_container(self) -> None:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=1),
        ):
            assert run_eval_in_docker(["all", "--free-only"]) == 1

    def test_uses_the_ci_image_tag(self) -> None:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["all"])
        assert DOCKER_IMAGE in streamed.call_args.args[0]

    def test_raises_when_docker_missing(self) -> None:
        with patch(f"{_MODULE}.shutil.which", return_value=None), pytest.raises(DockerUnavailableError):
            run_eval_in_docker(["all"])

    def test_build_failure_short_circuits_before_run(self) -> None:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=False),
            patch(f"{_MODULE}.run_streamed", return_value=2) as streamed,
        ):
            code = run_eval_in_docker(["all"])
        assert code == 2
        assert streamed.call_count == 1
        assert "build" in streamed.call_args.args[0]

    def test_image_present_probe_accepts_any_exit_code(self) -> None:
        with patch(f"{_MODULE}.run_allowed_to_fail", return_value=_completed(1)) as probe:
            assert _image_present() is False
        assert probe.call_args.kwargs["expected_codes"] is None
