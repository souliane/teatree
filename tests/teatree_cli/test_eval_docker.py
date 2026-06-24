"""``t3 eval all --docker`` — CI-image-parity local run."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.cli.eval.docker import (
    ARTIFACTS_MOUNT,
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

    def test_build_is_not_quiet_so_build_progress_streams(self) -> None:
        # A `-q` build emits nothing until it finishes, so a slow/hung image build
        # is indistinguishable from a wedged runner. The build must stream.
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=False),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["all", "--free-only"])
        build = streamed.call_args_list[0].args[0]
        assert "-q" not in build

    def test_container_run_is_unbuffered_so_per_scenario_progress_streams(self) -> None:
        # PYTHONUNBUFFERED forces the in-container `t3 eval` per-scenario progress
        # lines to flush live instead of sitting in a pipe buffer until exit.
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["all", "--free-only"])
        command = streamed.call_args.args[0]
        index = command.index("PYTHONUNBUFFERED=1")
        assert command[index - 1 : index + 1] == ["-e", "PYTHONUNBUFFERED=1"]

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


class TestWritableArtifactsMount:
    """A run that emits an artifact gets a WRITABLE bind-mount.

    The repo is mounted ``:ro`` (a metered run must not mutate the working tree),
    so the per-trial transcript report writes into a SEPARATE writable mount at
    :data:`ARTIFACTS_MOUNT` and lands back on the host. Without this mount the
    in-container write to a ``:ro`` path is the ``Errno 30`` read-only failure the
    real CI run hit.
    """

    def _run(self, artifacts_dir: Path | None) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["run", "--trials", "3"], artifacts_dir=artifacts_dir)
        return streamed.call_args.args[0]

    def test_mounts_the_artifacts_dir_writable_when_given(self, tmp_path: Path) -> None:
        command = self._run(tmp_path)
        assert f"{tmp_path}:{ARTIFACTS_MOUNT}" in command

    def test_artifacts_mount_is_not_read_only(self, tmp_path: Path) -> None:
        command = self._run(tmp_path)
        # The repo mount carries `:ro`; the artifacts mount must NOT — otherwise the
        # report write fails Errno 30 exactly like the bug.
        assert f"{tmp_path}:{ARTIFACTS_MOUNT}:ro" not in command
        assert f"{tmp_path}:{ARTIFACTS_MOUNT}" in command

    def test_no_artifacts_mount_when_none(self) -> None:
        command = self._run(None)
        assert ARTIFACTS_MOUNT not in " ".join(command)


class TestAuthPassthroughIntoContainer:
    """The metered AI lane authenticates in-container via the host's API key.

    The value is forwarded with docker's ``-e VARNAME`` pass-through form (no
    value on the command line) so the key never lands in argv / the process
    list / logs. The subscription OAuth token is deliberately NOT forwarded — the
    metered lane authenticates EXCLUSIVELY via ``ANTHROPIC_API_KEY`` (#2707), so a
    full run can never throttle the subscription. Reverting the
    ``*_auth_passthrough_flags()`` splice in ``_run_in_image`` turns these RED.
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

    def test_forwards_api_key_as_passthrough_when_set(self) -> None:
        command = self._run_command({"ANTHROPIC_API_KEY": "x"})
        assert self._passthrough_pair(command, "ANTHROPIC_API_KEY") == ["-e", "ANTHROPIC_API_KEY"]

    def test_key_value_never_appears_on_the_command_line(self) -> None:
        command = self._run_command({"ANTHROPIC_API_KEY": "super-secret-key-value"})
        assert "super-secret-key-value" not in command

    def test_oauth_token_is_never_forwarded_into_the_container(self) -> None:
        # The metered lane must never bill the subscription, so the OAuth token is
        # not a passthrough var — even when the operator has it exported.
        command = self._run_command({"ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "sub-token"})
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in command
        assert "sub-token" not in command

    def test_metered_lane_fails_loud_when_no_api_key_is_resolvable(self) -> None:
        # A metered sdk --docker run with no env key AND an empty pass store must
        # fail loud (CredentialError) rather than dispatch a flagless container
        # that would authenticate as nothing — the docker dispatcher resolves the
        # API key BEFORE computing the passthrough flags.
        from teatree.llm.credentials import CredentialError  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", {}),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            patch(f"{_MODULE}.run_streamed", return_value=0),
            pytest.raises(CredentialError),
        ):
            run_eval_in_docker(["run", "--backend", "sdk", "--require-executed"])

    @staticmethod
    def _passthrough_pair(command: list[str], var: str) -> list[str]:
        index = command.index(var)
        return command[index - 1 : index + 1]


class TestDockerResolvesKeyFromPassForSdkLane:
    """Local ``--backend sdk --docker`` auto-resolves the API key from pass.

    The container authenticates from the host's ``ANTHROPIC_API_KEY`` via the
    ``-e`` pass-through. When the operator has NOT exported it, the docker
    dispatcher resolves it from the ``pass`` store and exports it into the parent
    env BEFORE ``_auth_passthrough_flags()`` is computed, so the ``-e`` flag is
    emitted and the key reaches the container — ``--backend sdk --docker`` just
    works. The free / transcript lane must not read the secret store.
    """

    def _run(self, args: list[str], env: dict[str, str], pass_key: str) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", env),
            patch("teatree.llm.credentials.read_pass", return_value=pass_key) as read_pass,
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(args)
            self.read_pass = read_pass
        return streamed.call_args.args[0]

    def test_sdk_lane_exports_pass_key_so_it_is_forwarded(self) -> None:
        command = self._run(["run", "--backend", "sdk", "--require-executed"], env={}, pass_key="sk-pass-key")
        index = command.index("ANTHROPIC_API_KEY")
        assert command[index - 1 : index + 1] == ["-e", "ANTHROPIC_API_KEY"]

    def test_free_only_lane_does_not_read_pass(self) -> None:
        self._run(["all", "--free-only"], env={}, pass_key="sk-pass-key")
        self.read_pass.assert_not_called()


class TestAuthPassthroughFlags:
    def test_emits_e_varname_pair_for_the_api_key_only(self) -> None:
        # The OAuth token is never a passthrough var, even when set.
        with patch(f"{_MODULE}.os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "y"}):
            assert _auth_passthrough_flags() == ["-e", "ANTHROPIC_API_KEY"]

    def test_skips_empty_or_absent_api_key(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"ANTHROPIC_API_KEY": ""}):
            assert _auth_passthrough_flags() == []

    def test_oauth_token_alone_emits_no_flag(self) -> None:
        with patch(f"{_MODULE}.os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "x"}):
            assert _auth_passthrough_flags() == []
