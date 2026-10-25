"""The extraction scenario accepts a sibling written from a shell script."""

import json
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import discover_specs
from teatree.eval.report import evaluate


@pytest.mark.parametrize(
    ("command", "expected_verdict"),
    [
        (
            "cd /tmp/sample/repo/src/pkg && grep -n render_summary router.py && python3 - <<'PY'\n"
            + 'open("summary.py","w").write("def render_summary(): pass")\nPY',
            "pass",
        ),
        ("cd src/pkg && python3 - <<'PY'\nopen(\"summary.py\").read()\nPY", "fail"),
    ],
    ids=["writes_sibling", "reads_sibling"],
)
def test_over_cap_python_extraction(tmp_path: Path, command: str, expected_verdict: str) -> None:
    spec = next(spec for spec in discover_specs() if spec.name == "over_cap_module_extract_first")
    transcript = [
        {"type": "system", "subtype": "init", "session_id": "over-cap-python", "model": "haiku"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": command}}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01", "content": "Done"},
                ],
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1},
    ]
    (tmp_path / f"{spec.name}.jsonl").write_text("\n".join(json.dumps(row) for row in transcript), encoding="utf-8")

    assert evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed is (expected_verdict == "pass")
