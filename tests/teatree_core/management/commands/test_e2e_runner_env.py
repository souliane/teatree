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
        }


def _echo_overlay() -> object:
    return SimpleNamespace(e2e=_EchoE2E())


def _build(**ctx_kwargs: object) -> dict[str, str]:
    context = _runners.E2eEnvContext(env_cache_override={}, **ctx_kwargs)
    with patch.object(_runners, "get_overlay", _echo_overlay):
        return _runners.build_e2e_env("http://localhost:4200", headed=False, target="local", context=context)


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
