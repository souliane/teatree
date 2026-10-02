"""The runner owns target propagation, the artifacts dir, and the evidence flag (#3331)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.core.e2e_scenario import E2eExtrasContext
from teatree.core.management.commands import _e2e_runners as _runners


class _EchoE2E:
    """An overlay e2e seam that echoes the resolved context into env vars."""

    def env_extras(self, env_cache: dict[str, str], *, context: E2eExtrasContext) -> dict[str, str]:
        _ = env_cache
        return {
            "SEEN_TARGET": context.target,
            "SEEN_ARTIFACTS": context.artifacts_dir,
            "SEEN_SPEC": context.spec_path,
            "SEEN_COMPOSE": context.compose_project,
            "SEEN_BASE_URL": context.base_url,
        }


class _LocalModeE2E:
    def env_extras(self, env_cache: dict[str, str], *, context: E2eExtrasContext) -> dict[str, str]:
        _ = env_cache
        assert context.target == "local"
        return {
            "E2E_TARGET": "local",
            "CUSTOMER": "derived-local",
            "E2E_BROKERAGE_PASSWORD": "local-password",
        }


def _echo_overlay() -> object:
    return SimpleNamespace(e2e=_EchoE2E())


def _build(**ctx_kwargs: object) -> dict[str, str]:
    context = _runners.E2eEnvContext(env_cache_override={}, **ctx_kwargs)
    with patch.object(_runners, "get_overlay", _echo_overlay):
        return _runners.build_e2e_env("http://localhost:4200", target="local", context=context)


class TestArtifactsDirExport:
    def test_exports_artifacts_dir_when_set(self) -> None:
        env = _build(artifacts_dir="/tk/.t3-cache/artifacts")
        assert env[_runners.ARTIFACTS_ENV] == "/tk/.t3-cache/artifacts"

    def test_omits_artifacts_dir_when_empty(self) -> None:
        env = _build(artifacts_dir="")
        assert _runners.ARTIFACTS_ENV not in env


class TestEvidenceFlag:
    def test_managed_run_sets_evidence_flag(self) -> None:
        env = _build(capture_evidence=True)
        assert env[_runners.CAPTURE_EVIDENCE_ENV] == "1"

    def test_no_evidence_omits_the_flag(self) -> None:
        env = _build(capture_evidence=False)
        assert _runners.CAPTURE_EVIDENCE_ENV not in env


class TestTargetReachesTheSeam:
    def test_resolved_context_is_handed_to_env_extras(self) -> None:
        env = _build(artifacts_dir="/tk/a", test_path="e2e/login.spec.ts", compose_project="backend-wt7")
        # The overlay read the SAME target core routed at — not a BASE_URL guess.
        assert env["SEEN_TARGET"] == "local"
        assert env["SEEN_ARTIFACTS"] == "/tk/a"
        assert env["SEEN_SPEC"] == "e2e/login.spec.ts"
        assert env["SEEN_COMPOSE"] == "backend-wt7"

    def test_resolved_base_url_reaches_env_extras(self) -> None:
        # The seam context must carry the SAME BASE_URL the subprocess env gets —
        # an overlay reading os.environ instead sees the host process's env, not
        # this one, and can silently miss it (deferred fix from the last review).
        env = _build()
        assert env["SEEN_BASE_URL"] == "http://localhost:4200"

    def test_a_plain_local_run_keeps_the_callers_explicit_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CUSTOMER", "chosen-tenant")
        monkeypatch.setenv("E2E_BROKERAGE_PASSWORD", "chosen-password")
        monkeypatch.setenv("E2E_SELFSERVICE_USER", "chosen-user")
        context = _runners.E2eEnvContext(env_cache_override={}, run_target="local")

        with patch.object(_runners, "get_overlay", return_value=SimpleNamespace(e2e=_LocalModeE2E())):
            env = _runners.build_e2e_env("http://localhost:4200", target="local", context=context)

        assert env["CUSTOMER"] == "chosen-tenant"
        assert env["E2E_BROKERAGE_PASSWORD"] == "chosen-password"
        assert env["E2E_SELFSERVICE_USER"] == "chosen-user"

    def test_a_stack_run_drops_remote_secrets_and_keeps_the_chosen_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("E2E_TARGET", "dev")
        monkeypatch.setenv("CUSTOMER", "ambient-remote")
        monkeypatch.setenv("E2E_BROKERAGE_PASSWORD", "remote-password")
        monkeypatch.setenv("E2E_SELFSERVICE_USER", "chosen-user")
        context = _runners.E2eEnvContext(env_cache_override={}, run_target="stack")

        with patch.object(_runners, "get_overlay", return_value=SimpleNamespace(e2e=_LocalModeE2E())):
            env = _runners.build_e2e_env("http://localhost:4200", target="local", context=context)

        assert env["T3_E2E_TARGET"] == "local"
        assert env["E2E_TARGET"] == "local"
        assert env["CUSTOMER"] == "derived-local"
        assert env["E2E_BROKERAGE_PASSWORD"] == "local-password"
        assert env["E2E_SELFSERVICE_USER"] == "chosen-user"


class TestArtifactsRootDerivation:
    def test_root_is_out_of_repo_sibling(self) -> None:
        root = _runners.e2e_artifacts_root("/work/ticket-42/backend")
        assert root == Path("/work/ticket-42/.t3-cache/artifacts")


class TestRefuseArtifactsDirInRepo:
    def test_refuses_dir_inside_a_git_working_tree(self, tmp_path: Path) -> None:
        repo = tmp_path / "product"
        (repo / ".git").mkdir(parents=True)
        inside = repo / "artifacts"
        inside.mkdir()
        with pytest.raises(_runners.ArtifactsDirInRepoError):
            _runners.refuse_artifacts_dir_in_repo(inside)

    def test_allows_dir_outside_every_repo(self, tmp_path: Path) -> None:
        outside = tmp_path / "ticket" / ".t3-cache" / "artifacts"
        outside.mkdir(parents=True)
        _runners.refuse_artifacts_dir_in_repo(outside)  # no raise


class TestProjectRunnerManagedEnv:
    def test_managed_env_carries_artifacts_and_evidence(self) -> None:
        opts = _runners.ProjectRunOptions(resolved_target="dev", artifacts_dir="/tk/a", capture_evidence=True)
        env = _runners._managed_run_env(opts, "e2e.settings")
        assert env["T3_E2E_TARGET"] == "dev"
        assert env[_runners.ARTIFACTS_ENV] == "/tk/a"
        assert env[_runners.CAPTURE_EVIDENCE_ENV] == "1"

    def test_docker_flags_carry_the_managed_vars(self) -> None:
        opts = _runners.ProjectRunOptions(resolved_target="local", artifacts_dir="/tk/a", capture_evidence=False)
        flags = _runners._docker_managed_env_flags(opts)
        assert "T3_E2E_TARGET=local" in flags
        assert f"{_runners.ARTIFACTS_ENV}=/tk/a" in flags
        assert all("CAPTURE_EVIDENCE" not in f for f in flags)


_DASH_CONFIG = {
    "runner": "project",
    "test_dir": "e2e/dash",
    "settings_module": "e2e.dash.settings",
    "pytest_args": "-n0 -p no:randomly",
}


def _run_suite(checkout: Path, *, docker: bool) -> tuple[list[str], str]:
    overlay = SimpleNamespace(metadata=SimpleNamespace(get_e2e_config=lambda: dict(_DASH_CONFIG)))
    with (
        patch.object(_runners, "get_overlay", return_value=overlay),
        patch.object(_runners, "_project_worktree_path", return_value=str(checkout)),
        patch.object(_runners, "_DOCKERENV", checkout / "absent-dockerenv"),
        patch.object(_runners, "run_streamed", return_value=0) as run,
    ):
        _runners.run_project_suite(_runners.ProjectRunOptions(docker=docker), write_err=lambda _msg: None)
    return run.call_args.args[0], run.call_args.kwargs["cwd"]


class TestProjectSuiteCommand:
    def test_in_process_run_passes_the_settings_as_ds_and_the_overlay_pytest_args(self, tmp_path: Path) -> None:
        (tmp_path / "e2e" / "dash").mkdir(parents=True)
        argv, cwd = _run_suite(tmp_path, docker=False)
        assert argv[:4] == ["uv", "run", "pytest", "e2e/dash"]
        assert "--ds=e2e.dash.settings" in argv
        assert argv[argv.index("--ds=e2e.dash.settings") + 1 :][:3] == ["-n0", "-p", "no:randomly"]
        assert not any("DJANGO_SETTINGS_MODULE=" in token for token in argv)
        assert cwd == str(tmp_path)

    def test_compose_run_passes_the_same_suite_args_after_the_service(self, tmp_path: Path) -> None:
        (tmp_path / "e2e" / "dash").mkdir(parents=True)
        (tmp_path / "dev").mkdir()
        (tmp_path / "dev" / "docker-compose.yml").write_text("services: {}\n")
        argv, _ = _run_suite(tmp_path, docker=True)
        tail = argv[argv.index("e2e") + 1 :]
        assert tail == ["e2e/dash", "--ds=e2e.dash.settings", "-n0", "-p", "no:randomly"]

    def test_a_fork_vendoring_core_runs_the_suite_from_the_vendored_root(self, tmp_path: Path) -> None:
        vendored = tmp_path / "vendor" / "teatree"
        (vendored / "e2e" / "dash").mkdir(parents=True)
        (vendored / "dev").mkdir()
        (vendored / "dev" / "docker-compose.yml").write_text("services: {}\n")
        _, cwd = _run_suite(tmp_path, docker=False)
        compose_argv, compose_cwd = _run_suite(tmp_path, docker=True)
        assert cwd == compose_cwd == str(vendored)
        assert compose_argv[compose_argv.index("-f") + 1] == str(vendored / "dev" / "docker-compose.yml")
