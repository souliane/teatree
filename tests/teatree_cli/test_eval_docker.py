"""``t3 eval all --docker`` — CI-image-parity local run."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.cli.eval.docker import (
    ARTIFACTS_MOUNT,
    DOCKER_IMAGE,
    EVAL_CREDENTIAL_ENV_VAR,
    DockerUnavailableError,
    _auth_passthrough_flags,
    _image_present,
    _repo_root,
    run_eval_in_docker,
)
from teatree.core.models import AnthropicTokenUsage, ConfigSetting
from teatree.core.models.anthropic_token_usage import TokenHealthReading
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential

_MODULE = "teatree.cli.eval.docker"
_OAUTH_ENV = AnthropicSubscriptionCredential().spec.env_var
_API_KEY_ENV = AnthropicApiKeyCredential().spec.env_var
_METERED = "metered_api_key"


def _completed(returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    return proc


def _seed_oauth_routing() -> None:
    """Route the subscription credential to a configured account with a fresh healthy cache row.

    The subscription credential has NO built-in ``pass`` path (#124): the docker
    pre-export only reads the store when the ``anthropic_oauth_pass_paths`` routing
    selects an account. The fresh non-exhausted health row makes the selector reuse
    the cache — no rate-limit probe fires in the test.
    """
    route = "anthropic/test/oauth"
    ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", [route])
    reading = TokenHealthReading(
        organization_id="org-1",
        utilization_5h=0.1,
        utilization_7d=0.1,
        status_5h="allowed",
        status_7d="allowed",
        reset_5h=None,
        reset_7d=None,
    )
    AnthropicTokenUsage.objects.record(route, reading, now=timezone.now())


class TestRunEvalInDocker(TestCase):
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
        # benchmark is an always-fresh-run lane, so the eval-credential pre-export
        # fires; the default is subscription OAuth, so stub its export (its own auth
        # coverage is in the auth-passthrough class).
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch.object(AnthropicSubscriptionCredential, "export", return_value="oauth-test"),
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


class TestAuthPassthroughIntoContainer(TestCase):
    """The fresh-run AI lane authenticates in-container via the SELECTED credential.

    The value is forwarded with docker's ``-e VARNAME`` pass-through form (no value
    on the command line) so the credential never lands in argv / the process list /
    logs. The DEFAULT lane forwards the subscription ``CLAUDE_CODE_OAUTH_TOKEN``
    (reversing #2707); flipping the ``eval_credential`` knob (``T3_EVAL_CREDENTIAL``)
    to ``metered_api_key`` forwards ``ANTHROPIC_API_KEY`` instead. Reverting the
    ``*_auth_passthrough_flags()`` splice in ``_run_in_image`` turns these RED.
    """

    def _run_command(self, env: dict[str, str]) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", env),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["run", "--backend", "api", "--require-executed"])
        return streamed.call_args.args[0]

    def test_default_lane_forwards_the_oauth_token_as_passthrough(self) -> None:
        command = self._run_command({_OAUTH_ENV: "oauth-sub"})
        assert self._passthrough_pair(command, _OAUTH_ENV) == ["-e", _OAUTH_ENV]

    def test_oauth_value_never_appears_on_the_command_line(self) -> None:
        command = self._run_command({_OAUTH_ENV: "super-secret-oauth-value"})
        assert "super-secret-oauth-value" not in command

    def test_default_lane_does_not_forward_the_metered_key(self) -> None:
        # The default subscription lane must not leak/forward the metered API key.
        command = self._run_command({_OAUTH_ENV: "oauth-sub", _API_KEY_ENV: "sk-metered"})
        assert _API_KEY_ENV not in command
        assert "sk-metered" not in command

    def test_metered_knob_forwards_the_api_key_not_the_oauth_token(self) -> None:
        command = self._run_command(
            {EVAL_CREDENTIAL_ENV_VAR: _METERED, _API_KEY_ENV: "sk-metered", _OAUTH_ENV: "oauth-sub"}
        )
        assert self._passthrough_pair(command, _API_KEY_ENV) == ["-e", _API_KEY_ENV]
        assert _OAUTH_ENV not in command

    def test_credential_knob_override_is_forwarded_into_the_container(self) -> None:
        # The in-container re-invocation must see the same knob so it resolves the
        # same credential kind, without depending on a ConfigSetting row.
        command = self._run_command({EVAL_CREDENTIAL_ENV_VAR: _METERED, _API_KEY_ENV: "sk-metered"})
        assert self._passthrough_pair(command, EVAL_CREDENTIAL_ENV_VAR) == ["-e", EVAL_CREDENTIAL_ENV_VAR]

    def test_default_lane_fails_loud_when_no_oauth_token_is_resolvable(self) -> None:
        # A default (OAuth) api --docker run with no token AND an empty pass store
        # must fail loud (CredentialError) rather than dispatch a flagless container
        # — the docker dispatcher resolves the credential BEFORE the passthrough flags.
        from teatree.llm.credentials import CredentialError  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", {}),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            patch(f"{_MODULE}.run_streamed", return_value=0),
            pytest.raises(CredentialError),
        ):
            run_eval_in_docker(["run", "--backend", "api", "--require-executed"])

    @staticmethod
    def _passthrough_pair(command: list[str], var: str) -> list[str]:
        index = command.index(var)
        return command[index - 1 : index + 1]


class TestHeadShaPassthroughIntoContainer(TestCase):
    """``GITHUB_SHA`` is forwarded so the in-container ``--summary-json`` records the SHA.

    The publish-safe JSON is written IN the container, so its ``head_sha`` reads the
    forwarded ``GITHUB_SHA``. It rides docker's ``-e VARNAME`` pass-through (value
    via env, never argv) and is forwarded only when GitHub Actions set it — a host
    run with no ``GITHUB_SHA`` adds no flag.
    """

    def _run_command(self, env: dict[str, str]) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", env),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["run", "--backend", "api", "--require-executed"])
        return streamed.call_args.args[0]

    def test_github_sha_is_forwarded_as_passthrough_when_set(self) -> None:
        command = self._run_command({_OAUTH_ENV: "oauth-sub", "GITHUB_SHA": "deadbeef"})
        index = command.index("GITHUB_SHA")
        assert command[index - 1 : index + 1] == ["-e", "GITHUB_SHA"]
        assert "deadbeef" not in command  # value rides the env, never argv

    def test_github_sha_flag_absent_when_unset(self) -> None:
        assert "GITHUB_SHA" not in self._run_command({_OAUTH_ENV: "oauth-sub"})


class TestBenchmarkLaneFailsLoudBeforeDocker(TestCase):
    """``t3 eval benchmark`` is always a fresh run, so the Docker pre-export fires for it too.

    The benchmark argv starts ``benchmark`` and carries no literal ``api`` token,
    yet the lane always runs a model. The eval-credential pre-export must therefore
    key on the fresh-run SUBCOMMAND, not only the ``api`` backend token — a missing
    credential must fail loud with :class:`~teatree.llm.credentials.CredentialError`
    BEFORE the container is built or run. Narrowing the detector back to the ``api``
    token alone turns these RED.
    """

    def test_keyless_benchmark_raises_before_any_docker_call(self) -> None:
        from teatree.llm.credentials import CredentialError  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True) as image_present,
            patch(f"{_MODULE}.os.environ", {}),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
            pytest.raises(CredentialError),
        ):
            run_eval_in_docker(["benchmark", "--models", "claude-opus-4-8@xhigh"])
        # Fail loud BEFORE doing any work: neither the image probe nor a build/run ran.
        image_present.assert_not_called()
        streamed.assert_not_called()

    def test_benchmark_forwards_the_resolved_oauth_token_into_the_container(self) -> None:
        _seed_oauth_routing()
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", {}),
            patch("teatree.llm.credentials.read_pass", return_value="oauth-pass-token"),
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(["benchmark", "--models", "claude-opus-4-8@xhigh"])
        command = streamed.call_args.args[0]
        index = command.index(_OAUTH_ENV)
        assert command[index - 1 : index + 1] == ["-e", _OAUTH_ENV]


class TestDockerResolvesCredentialFromPassForSdkLane(TestCase):
    """Local ``--backend api --docker`` auto-resolves the credential from pass.

    The container authenticates from the host's SELECTED eval credential via the
    ``-e`` pass-through. When the operator has NOT exported it, the docker dispatcher
    resolves it from the ``pass`` store and exports it into the parent env BEFORE
    ``_auth_passthrough_flags()`` is computed, so the ``-e`` flag is emitted and the
    credential reaches the container — ``--backend api --docker`` just works. The
    free / transcript lane must not read the secret store.
    """

    def _run(self, args: list[str], env: dict[str, str], pass_value: str) -> list[str]:
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{_MODULE}._image_present", return_value=True),
            patch(f"{_MODULE}.os.environ", env),
            patch("teatree.llm.credentials.read_pass", return_value=pass_value) as read_pass,
            patch(f"{_MODULE}.run_streamed", return_value=0) as streamed,
        ):
            run_eval_in_docker(args)
            self.read_pass = read_pass
        return streamed.call_args.args[0]

    def test_api_lane_exports_pass_oauth_token_so_it_is_forwarded(self) -> None:
        _seed_oauth_routing()
        command = self._run(["run", "--backend", "api", "--require-executed"], env={}, pass_value="oauth-pass-token")
        index = command.index(_OAUTH_ENV)
        assert command[index - 1 : index + 1] == ["-e", _OAUTH_ENV]

    def test_free_only_lane_does_not_read_pass(self) -> None:
        self._run(["all", "--free-only"], env={}, pass_value="oauth-pass-token")
        self.read_pass.assert_not_called()


class TestAuthPassthroughFlags:
    def test_forwards_the_selected_credential_var_when_set(self) -> None:
        with patch(f"{_MODULE}.os.environ", {_OAUTH_ENV: "x", _API_KEY_ENV: "y"}):
            assert _auth_passthrough_flags((_OAUTH_ENV,)) == ["-e", _OAUTH_ENV]

    def test_skips_the_selected_var_when_empty_or_absent(self) -> None:
        with patch(f"{_MODULE}.os.environ", {_OAUTH_ENV: ""}):
            assert _auth_passthrough_flags((_OAUTH_ENV,)) == []

    def test_a_non_selected_var_present_in_env_is_not_forwarded(self) -> None:
        # Only the SELECTED credential var (passed in) is forwarded — a stray token
        # for the other credential in the env is never spliced in.
        with patch(f"{_MODULE}.os.environ", {_API_KEY_ENV: "x"}):
            assert _auth_passthrough_flags((_OAUTH_ENV,)) == []

    def test_forwards_the_credential_knob_override_when_set(self) -> None:
        with patch(f"{_MODULE}.os.environ", {_OAUTH_ENV: "x", EVAL_CREDENTIAL_ENV_VAR: _METERED}):
            assert _auth_passthrough_flags((_OAUTH_ENV,)) == ["-e", _OAUTH_ENV, "-e", EVAL_CREDENTIAL_ENV_VAR]
