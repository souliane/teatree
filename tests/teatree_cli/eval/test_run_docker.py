"""``RunDockerArgs`` — the ``t3 eval run`` flags forwarded into the CI image.

The metered ``run`` lane re-invokes ``t3 eval run`` inside the CI container. The
``--transcript-html`` host path is translated to a container path under the
writable ``/artifacts`` bind-mount, so the report the in-container run writes
lands back on the host for upload. These tests pin that translation and the
writable-dir resolution.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from teatree.cli.eval.docker import ARTIFACTS_MOUNT
from teatree.cli.eval.run_docker import RunDockerArgs


def _args(**overrides: object) -> RunDockerArgs:
    base: dict[str, object] = {
        "name": None,
        "lane": None,
        "shard": None,
        "output_format": "text",
        "max_turns": None,
        "max_budget_usd": 1.0,
        "effort": "high",
        "trials": 3,
        "require": "any",
        "models": None,
        "backend": "api",
        "require_executed": True,
        "parallel": 1,
    }
    base.update(overrides)
    return RunDockerArgs(**base)


class TestTranscriptHtmlPassthrough:
    def test_translates_host_path_to_the_artifacts_mount(self) -> None:
        args = _args(transcript_html=Path("/home/runner/_temp/eval-transcripts.html"))
        passthrough = args.passthrough()
        index = passthrough.index("--transcript-html")
        assert passthrough[index + 1] == f"{ARTIFACTS_MOUNT}/eval-transcripts.html"

    def test_omits_the_flag_when_no_artifact_requested(self) -> None:
        assert "--transcript-html" not in _args(transcript_html=None).passthrough()

    def test_still_forces_no_persist(self) -> None:
        # The container is ephemeral, so the run stays --no-persist regardless of
        # the new artifact flag — the artifact is the durable output, not the ledger.
        assert "--no-persist" in _args(transcript_html=Path("/tmp/x.html")).passthrough()


class TestEscalationPassthrough:
    def test_forwards_escalate_on_fail_with_trials_into_the_container(self) -> None:
        # The PR lane's single trial runs in --docker, so the escalation flags must
        # cross the container boundary or the in-container run reds immediately on
        # the first failure instead of escalating.
        passthrough = _args(trials=1, escalate_on_fail=True, escalate_trials=3).passthrough()
        assert "--escalate-on-fail" in passthrough
        index = passthrough.index("--escalate-trials")
        assert passthrough[index + 1] == "3"

    def test_omits_the_flag_when_escalation_is_off(self) -> None:
        assert "--escalate-on-fail" not in _args(trials=1, escalate_on_fail=False).passthrough()


class TestSummaryMdPassthrough:
    def test_translates_host_path_to_the_artifacts_mount(self) -> None:
        args = _args(summary_md=Path("/home/runner/_temp/step-summary.md"))
        passthrough = args.passthrough()
        index = passthrough.index("--summary-md")
        assert passthrough[index + 1] == f"{ARTIFACTS_MOUNT}/step-summary.md"

    def test_omits_the_flag_when_no_summary_requested(self) -> None:
        assert "--summary-md" not in _args(summary_md=None).passthrough()

    def test_container_summary_path_is_empty_when_no_summary_requested(self) -> None:
        # The in-container redirect resolves to "" when no --summary-md was asked
        # for — the no-artifact branch of the path translation.
        assert _args(summary_md=None)._container_summary_path() == ""

    def test_container_summary_path_redirects_to_the_mount_when_requested(self) -> None:
        translated = _args(summary_md=Path("/runner/_temp/dash.md"))._container_summary_path()
        assert translated == f"{ARTIFACTS_MOUNT}/dash.md"

    def test_summary_only_run_still_resolves_an_artifacts_dir(self, tmp_path: Path) -> None:
        # The summary-md path's PARENT is the writable bind-mount even when no
        # transcript-html is requested — the summary-only lane must still mount it.
        host = tmp_path / "step-summary.md"
        with (
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_in_docker,
            pytest.raises(typer.Exit),
        ):
            _args(transcript_html=None, summary_md=host).dispatch()
        assert run_in_docker.call_args.kwargs["artifacts_dir"] == tmp_path


class TestSummaryJsonPassthrough:
    def test_translates_host_path_to_the_artifacts_mount(self) -> None:
        args = _args(summary_json=Path("/home/runner/_temp/eval-heal.json"))
        passthrough = args.passthrough()
        index = passthrough.index("--summary-json")
        assert passthrough[index + 1] == f"{ARTIFACTS_MOUNT}/eval-heal.json"

    def test_omits_the_flag_when_no_json_requested(self) -> None:
        assert "--summary-json" not in _args(summary_json=None).passthrough()

    def test_json_only_run_still_resolves_an_artifacts_dir(self, tmp_path: Path) -> None:
        host = tmp_path / "eval-heal.json"
        with (
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_in_docker,
            pytest.raises(typer.Exit),
        ):
            _args(transcript_html=None, summary_md=None, summary_json=host).dispatch()
        assert run_in_docker.call_args.kwargs["artifacts_dir"] == tmp_path


class TestDispatchMountsHostParentDir:
    def test_dispatch_passes_the_host_parent_dir_as_artifacts_dir(self, tmp_path: Path) -> None:
        host = tmp_path / "eval-transcripts.html"
        with (
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_in_docker,
            pytest.raises(typer.Exit),
        ):
            _args(transcript_html=host).dispatch()
        assert run_in_docker.call_args.kwargs["artifacts_dir"] == tmp_path

    def test_dispatch_passes_none_artifacts_dir_without_transcript(self) -> None:
        with (
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_in_docker,
            pytest.raises(typer.Exit),
        ):
            _args(transcript_html=None, summary_md=None).dispatch()
        assert run_in_docker.call_args.kwargs["artifacts_dir"] is None
