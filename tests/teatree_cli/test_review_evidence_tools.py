"""The worker-facing command records a head-bound review evidence receipt."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.kill_matrix import KillMatrix
from teatree.quality.review_evidence import ReviewEvidence


def test_review_evidence_command_writes_measured_head() -> None:
    head = "a" * 40
    evidence = ReviewEvidence(head, (), KillMatrix(head, ()))
    with (
        patch("teatree.quality.review_evidence.measure_review_evidence", return_value=evidence) as measure,
        patch("teatree.quality.review_evidence.write_review_evidence") as write,
    ):
        result = CliRunner().invoke(app, ["tool", "review-evidence", "--expect-sha", head])
    assert result.exit_code == 0
    assert head in result.output
    assert measure.call_args.kwargs["expect_sha"] == head
    write.assert_called_once()
