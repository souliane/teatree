"""``GhCiEvalClient`` builds the right ``gh`` argv and never puts the token on it.

The client is the only place loop/CLI code touches ``gh`` for the CI-eval heal
loop, so these pin the exact argv each method builds (workflow run / run list /
run view / run download / commit-sha resolve) and — security-critical — that the
token is passed through the ``token`` kwarg (which ``_run_gh`` injects into the
env) and NEVER appears on the command line. The ``gh`` subprocess is the one
unstoppable external, stubbed at the ``_run_gh`` seam.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.backends.github import ci_eval_client
from teatree.backends.github.ci_eval_client import GhCiEvalClient


class _Recorder:
    """A ``_run_gh`` replacement that records argv + token and returns canned stdout."""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.args: tuple[str, ...] = ()
        self.token: str = ""

    def __call__(self, *args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
        self.args = args
        self.token = token
        return CompletedProcess(args=list(args), returncode=0, stdout=self.stdout, stderr="")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(ci_eval_client, "_run_gh", rec)
    return rec


class TestTriggerWorkflow:
    def test_builds_workflow_run_argv_with_ref_and_inputs(self, recorder: _Recorder) -> None:
        GhCiEvalClient("souliane/teatree").trigger_workflow(
            "eval-ci-heal.yml", ref="fix-branch", inputs={"scenarios": "a,b", "credential": "subscription_oauth"}
        )
        assert recorder.args == (
            "gh",
            "workflow",
            "run",
            "eval-ci-heal.yml",
            "--repo",
            "souliane/teatree",
            "--ref",
            "fix-branch",
            "-f",
            "scenarios=a,b",
            "-f",
            "credential=subscription_oauth",
        )

    def test_token_is_never_on_the_command_line(self, recorder: _Recorder) -> None:
        GhCiEvalClient("souliane/teatree", token="ghp_SECRET").trigger_workflow("eval-ci-heal.yml", ref="b", inputs={})
        assert recorder.token == "ghp_SECRET"
        assert "ghp_SECRET" not in recorder.args


class TestResolveHeadSha:
    def test_reads_the_commit_sha_via_gh_api(self, recorder: _Recorder) -> None:
        recorder.stdout = "abc123\n"
        sha = GhCiEvalClient("souliane/teatree").resolve_head_sha("fix-branch")
        assert sha == "abc123"
        assert recorder.args == (
            "gh",
            "api",
            "repos/souliane/teatree/commits/fix-branch",
            "--jq",
            ".sha",
        )


class TestListRuns:
    def test_requests_the_fsm_fields_for_the_branch(self, recorder: _Recorder) -> None:
        recorder.stdout = '[{"databaseId": 42, "headSha": "abc", "status": "completed", "conclusion": "failure"}]'
        runs = GhCiEvalClient("souliane/teatree").list_runs("eval-ci-heal.yml", branch="fix-branch", limit=5)
        assert runs == [{"databaseId": 42, "headSha": "abc", "status": "completed", "conclusion": "failure"}]
        assert "--json" in recorder.args
        assert recorder.args[recorder.args.index("--json") + 1] == "databaseId,headSha,status,conclusion,createdAt"
        assert recorder.args[recorder.args.index("--limit") + 1] == "5"

    def test_empty_stdout_yields_empty_list(self, recorder: _Recorder) -> None:
        recorder.stdout = ""
        assert GhCiEvalClient("souliane/teatree").list_runs("eval-ci-heal.yml", branch="b") == []


class TestViewRun:
    def test_returns_the_verdict_dict(self, recorder: _Recorder) -> None:
        recorder.stdout = '{"status": "completed", "conclusion": "success", "headSha": "abc", "url": "u"}'
        run = GhCiEvalClient("souliane/teatree").view_run(42)
        assert run == {"status": "completed", "conclusion": "success", "headSha": "abc", "url": "u"}
        assert recorder.args[:4] == ("gh", "run", "view", "42")


class TestDownloadArtifact:
    def test_builds_run_download_argv(self, recorder: _Recorder, tmp_path: Path) -> None:
        GhCiEvalClient("souliane/teatree").download_artifact(42, name="eval-heal-abc", dest_dir=tmp_path)
        assert recorder.args == (
            "gh",
            "run",
            "download",
            "42",
            "--repo",
            "souliane/teatree",
            "--name",
            "eval-heal-abc",
            "--dir",
            str(tmp_path),
        )


class TestBuildFactory:
    def test_reads_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_env")
        assert ci_eval_client.build_ci_eval_client("owner/repo").token == "ghp_env"

    def test_defaults_to_the_public_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        client = ci_eval_client.build_ci_eval_client()
        assert client.repo == "souliane/teatree"
        assert client.token == ""
