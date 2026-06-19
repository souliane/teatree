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
        "backend": "sdk",
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
            _args(transcript_html=None).dispatch()
        assert run_in_docker.call_args.kwargs["artifacts_dir"] is None
