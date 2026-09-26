from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.report import evaluate

_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _grade(scenario: str, fixture: str, tmp_path: Path) -> bool:
    spec = find_spec(scenario)
    assert spec is not None, f"scenario {scenario!r} not discovered"
    source = _FIXTURES / f"{fixture}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


def test_line_continued_severity_label_on_an_approved_mr_is_caught(tmp_path: Path) -> None:
    scenario = "review_approval_posts_nonblocker_without_severity"
    assert _grade(scenario, f"{scenario}_line_continued_fail", tmp_path) is False


def test_line_continued_blocking_post_without_approval_passes(tmp_path: Path) -> None:
    scenario = "review_blocker_withholds_approval"
    assert _grade(scenario, f"{scenario}_line_continued_pass", tmp_path) is True
