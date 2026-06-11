"""``t3 eval all --docker`` — CI-image-parity local run."""

from unittest.mock import MagicMock, patch

import pytest

from teatree.cli.eval.docker import (
    DOCKER_IMAGE,
    DockerUnavailableError,
    _auth_passthrough_flags,
    _image_present,
    _repo_root,
    run_eval_in_docker,
)

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

    def test_sets_in_container_marker_so_the_re_invoked_command_runs_in_process(self) -> None:
        # The in-container `t3 eval` re-invocation must run DIRECTLY in-process, not
        # re-route to docker again (an infinite loop). The marker breaks the loop.
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["benchmark", "--models", "claude-opus-4-8@xhigh"])
        command = streamed.call_args.args[0]
        index = command.index("T3_EVAL_IN_CONTAINER=1")
        assert command[index - 1 : index + 1] == ["-e", "T3_EVAL_IN_CONTAINER=1"]

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

    def test_repo_root_is_the_build_context_holding_the_dockerfile(self) -> None:
        # The build runs `-f dev/Dockerfile.test .` from this root; a src-layout
        # off-by-one (parents[3] -> src/) would mount a context with no Dockerfile.
        root = _repo_root()
        assert (root / "dev" / "Dockerfile.test").is_file()
        assert (root / "pyproject.toml").is_file()


class TestAuthPassthroughIntoContainer:
    """The metered AI lane authenticates in-container via the host's OAuth token.

    The value is forwarded with docker's ``-e VARNAME`` pass-through form (no
    value on the command line) so the token never lands in argv / the process
    list / logs. Reverting the ``*_auth_passthrough_flags()`` splice in
    ``_run_in_image`` turns these RED.
    """

    def _run_command(self, env: dict[str, str]) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", env),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["run", "--backend", "sdk", "--require-executed"])
        return streamed.call_args.args[0]

    def test_forwards_oauth_token_as_passthrough_when_set(self) -> None:
        command = self._run_command({"CLAUDE_CODE_OAUTH_TOKEN": "x"})
        assert self._passthrough_pair(command, "CLAUDE_CODE_OAUTH_TOKEN") == ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]

    def test_token_value_never_appears_on_the_command_line(self) -> None:
        command = self._run_command({"CLAUDE_CODE_OAUTH_TOKEN": "super-secret-token-value"})
        assert "super-secret-token-value" not in command

    def test_forwards_api_key_as_passthrough_when_set(self) -> None:
        command = self._run_command({"ANTHROPIC_API_KEY": "x"})
        assert self._passthrough_pair(command, "ANTHROPIC_API_KEY") == ["-e", "ANTHROPIC_API_KEY"]

    def test_no_auth_flag_when_neither_credential_is_set(self) -> None:
        command = self._run_command({})
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in command
        assert "ANTHROPIC_API_KEY" not in command

    @staticmethod
    def _passthrough_pair(command: list[str], var: str) -> list[str]:
        index = command.index(var)
        return command[index - 1 : index + 1]


class TestDockerResolvesTokenFromPassForSdkLane:
    """Local ``--backend sdk --docker`` auto-resolves the OAuth token from pass.

    The container authenticates from the host's ``CLAUDE_CODE_OAUTH_TOKEN`` via
    the ``-e`` pass-through. When the operator has NOT exported it, the docker
    dispatcher resolves it from the ``pass`` store and exports it into the parent
    env BEFORE ``_auth_passthrough_flags()`` is computed, so the ``-e`` flag is
    emitted and the token reaches the container — ``--backend sdk --docker`` just
    works. The free / subscription lane must not read the secret store.
    """

    def _run(self, args: list[str], env: dict[str, str], pass_token: str) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", env),
            patch("teatree.eval.auth.os.environ", env),
            patch("teatree.eval.auth.read_pass", return_value=pass_token) as read_pass,
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(args)
            self.read_pass = read_pass
        return streamed.call_args.args[0]

    def test_sdk_lane_exports_pass_token_so_it_is_forwarded(self) -> None:
        command = self._run(["run", "--backend", "sdk", "--require-executed"], env={}, pass_token="pass-tok")
        index = command.index("CLAUDE_CODE_OAUTH_TOKEN")
        assert command[index - 1 : index + 1] == ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]

    def test_free_only_lane_does_not_read_pass(self) -> None:
        self._run(["all", "--free-only"], env={}, pass_token="pass-tok")
        self.read_pass.assert_not_called()


class TestAuthPassthroughFlags:
    def test_emits_e_varname_pairs_for_present_vars_only(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "y"}):
            assert _auth_passthrough_flags() == ["-e", "CLAUDE_CODE_OAUTH_TOKEN", "-e", "ANTHROPIC_API_KEY"]

    def test_skips_empty_or_absent_vars(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"ANTHROPIC_API_KEY": ""}):
            assert _auth_passthrough_flags() == []

    def test_oauth_token_is_preferred_first(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "x"}):
            assert _auth_passthrough_flags() == ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]
