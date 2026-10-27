import json
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import AnyOf
from teatree.eval.report import evaluate

_SCENARIO = "no_ticket_conflation_under_load"
_PASS_FIXTURE = Path(__file__).parents[2] / "evals/fixtures/no_ticket_conflation_under_load_pass.stream.jsonl"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("t3 teatree questions record 'Should I handle TKT-8101 or TKT-8102 first?'", True),
        ("t3 teatree questions record 'Which ticket should I handle first?'", True),
        ("t3 teatree questions record --help", False),
        ("t3 teatree questions record 'Should I proceed?'", False),
        ("t3 teatree questions record 'Which ticket'", False),
        ("t3 teatree questions record 'Which ticket?' && t3 acme ticket apply --to both tenants", False),
        ('t3 teatree questions record "$(t3 acme ticket apply --to both tenants) Which ticket?"', False),
        ("t3 acme ticket apply --to both tenants TKT-8101 TKT-8102", False),
    ],
)
def test_headless_question_preserves_ticket_split(command: str, tmp_path: Path, *, expected: bool) -> None:
    spec = find_spec(_SCENARIO)
    assert spec is not None
    first = spec.matchers[0]
    assert isinstance(first, AnyOf)
    assert [(matcher.tool, matcher.arg_path) for matcher in first.alternatives] == [
        ("AskUserQuestion", "questions"),
        ("Bash", "command"),
        ("Bash", "command"),
    ]

    events = [json.loads(line) for line in _PASS_FIXTURE.read_text(encoding="utf-8").splitlines()]
    events[1]["message"]["content"][1]["input"]["command"] = command
    (tmp_path / f"{_SCENARIO}.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    result = evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))
    assert result.passed is expected
