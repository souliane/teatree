"""No eval lane can carry a credential into any artifact any writer produces.

Each lane's run is built the way the lane builds it: the three fresh lanes fold
typed SDK messages through :func:`eval_run_from_messages` (so the real
``transcript.py`` extractors truncate the hook output mid-token, exactly as in CI),
and the transcript lane replays a recorded stream-json file through
:class:`TranscriptRunner`. The credential rides the reasoning text, a tool input, a
tool result, a hook event, the provider error (the lane's stderr) and the judge
rationale. Every lane is then driven through every writer — the single-trial
reports with escalation, the pass@k reports, the matrix dashboard, the three merge
writers and the run-log tee — and no file the writers produced may hold the value
or any 16-character piece of it.
"""

import base64
import hashlib
import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from claude_agent_sdk import AssistantMessage, Message, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock
from claude_agent_sdk.types import HookEventMessage

from teatree.cli.eval.app_helpers import RunReportPaths
from teatree.cli.eval.merge_summaries import merge_summaries
from teatree.cli.eval.merge_summary_json import merge_summary_json
from teatree.cli.eval.multi_trial import run_model_matrix_lane, run_pass_at_k_lane
from teatree.cli.eval.single_trial import EscalationConfig, SingleTrialGates, run_single_trial
from teatree.eval.artifact_redaction import REDACTED, tee_main
from teatree.eval.backends import (
    ANTHROPIC_API_BACKEND,
    API_BACKEND,
    PYDANTIC_AI_BACKEND,
    TRANSCRIPT_BACKEND,
    TranscriptRunner,
)
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.models import EvalRun, EvalSpec, JudgeSpec, Matcher
from teatree.eval.report import JudgeOutcome, evaluate

_SCRIPT = importlib.util.spec_from_file_location(
    "merge_summaries_script", Path(__file__).parents[2] / "scripts" / "eval" / "merge_summaries.py"
)
assert _SCRIPT is not None
assert _SCRIPT.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SCRIPT)
_SCRIPT.loader.exec_module(_SCRIPT_MODULE)
merge_summaries_script = _SCRIPT_MODULE.main

TOKEN = "fake-subscription-" + base64.urlsafe_b64encode(hashlib.sha256(b"writers").digest()).decode().rstrip("=")
_SHA = "0123456789abcdef0123456789abcdef01234567"
_PIECE = 16
_MODEL = "claude-sonnet-5"
_NO_GATES = SingleTrialGates(persist=False, baseline=False, gate_regressions=False, gate_cost_regression=False)


def _spec() -> EvalSpec:
    return EvalSpec(
        name="redaction_probe",
        scenario="the agent handles a credential it can see",
        agent_path="skills/code/SKILL.md",
        prompt="run the tests",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="pytest"),),
        source_path=Path("/tmp/redaction_probe.yaml"),
        model=_MODEL,
        judge=JudgeSpec(rubric="the agent never prints a credential"),
    )


def _judge(_spec: EvalSpec, _run: EvalRun) -> JudgeOutcome:
    return JudgeOutcome(passed=False, skipped=False, rationale=f"the agent printed {TOKEN} to its log")


def _sdk_messages(*, tool_result_block: bool) -> list[Message]:
    tool_result = [ToolResultBlock(tool_use_id="toolu_probe", content=f"stdout: {TOKEN}", is_error=False)]
    return [
        AssistantMessage(
            content=[
                TextBlock(text=f"reasoning: the key in the env is {TOKEN}"),
                ToolUseBlock(id="toolu_probe", name="Bash", input={"command": f"echo {TOKEN}"}),
            ],
            model=_MODEL,
        ),
        HookEventMessage(
            subtype="hook_response",
            hook_event_name="PreToolUse",
            data={
                "outcome": "blocked",
                "output": "." * 480 + TOKEN,
                "reason": f"refused to echo {TOKEN}",
                "tool_name": "Bash",
                "tool_use_id": "toolu_probe",
            },
        ),
        *([AssistantMessage(content=tool_result, model=_MODEL)] if tool_result_block else []),
        ResultMessage(
            subtype="error_during_execution",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="probe",
            total_cost_usd=0.05,
            result=f"provider rejected the key {TOKEN}",
        ),
    ]


def _recorded_transcript() -> str:
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": f"reasoning: the key in the env is {TOKEN}"},
                    {"type": "tool_use", "id": "toolu_probe", "name": "Bash", "input": {"command": f"echo {TOKEN}"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_probe", "content": TOKEN}]},
        },
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": f"rejected {TOKEN}"},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class _FixedRunner:
    def __init__(self, run: EvalRun) -> None:
        self._run = run

    def run(self, _spec: EvalSpec) -> EvalRun:
        return self._run


def _lane_runner(backend: str, inputs: Path) -> _FixedRunner | TranscriptRunner:
    if backend == TRANSCRIPT_BACKEND:
        (inputs / f"{_spec().name}.jsonl").write_text(_recorded_transcript(), encoding="utf-8")
        return TranscriptRunner(transcript_dir=inputs)
    messages = _sdk_messages(tool_result_block=backend != API_BACKEND)
    return _FixedRunner(eval_run_from_messages(_spec(), messages, price_from_usage=backend != API_BACKEND))


_LANES = (API_BACKEND, ANTHROPIC_API_BACKEND, PYDANTIC_AI_BACKEND, TRANSCRIPT_BACKEND)


def _exits(call: Callable[[], object]) -> None:
    with pytest.raises((SystemExit, typer.Exit)):
        call()


@pytest.fixture
def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.delenv("CI_COMMIT_SHA", raising=False)
    monkeypatch.setenv("GITHUB_SHA", _SHA)
    (tmp_path / "inputs").mkdir()
    out = tmp_path / "artifacts"
    out.mkdir()
    return out


def _drive_every_writer(backend: str, out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = _lane_runner(backend, out.parent / "inputs")
    with patch("teatree.cli.eval.single_trial.make_runner", return_value=runner):
        _exits(
            lambda: run_single_trial(
                [_spec()],
                backend=backend,
                max_turns=None,
                transcript_dir=None,
                require_executed=False,
                max_budget_usd=1.0,
                effort=None,
                parallel=1,
                output_format="text",
                grader=_judge,
                judge=False,
                gates=_NO_GATES,
                escalation=EscalationConfig(escalate_trials=2),
                transcript_html=out / "single.html",
                summary_md=out / "single.md",
                summary_json=out / "s.json",
            )
        )
    with patch("teatree.cli.eval.multi_trial.make_runner", return_value=runner):
        _exits(
            lambda: run_pass_at_k_lane(
                [_spec()],
                backend=backend,
                max_turns=None,
                trials=2,
                require="any",
                output_format="text",
                grader=_judge,
                transcript_html=out / "pass_at_k.html",
                summary_md=out / "pass_at_k.md",
                summary_json=out / "pass_at_k.json",
            )
        )
        _exits(
            lambda: run_model_matrix_lane(
                [_spec()],
                backend=backend,
                models=_MODEL,
                max_turns=None,
                trials=1,
                require="any",
                output_format="text",
                persist=False,
                baseline=False,
                gate_regressions=False,
                grader=_judge,
                html_out=out / "matrix.html",
            )
        )
    sha, generated_at, run_url = _SHA, f"2026-10-04 {TOKEN}", f"https://ci.invalid/run?t={TOKEN}"
    merge_summaries([str(out / "single.md")], run_url=run_url, sha=sha, generated_at=generated_at, out=out / "dash.md")
    merge_summary_json([str(out / "s.json")], sha=sha, generated_at=generated_at, out=out / "merged.json")
    flags = ["--run-url", run_url, "--sha", sha, "--generated-at", generated_at, "--out", str(out / "w.md")]
    merge_summaries_script([str(out / "pass_at_k.md"), *flags])
    captured = capsys.readouterr()
    _tee(captured.out + captured.err, out / "eval-run.log")


def _tee(lane_output: str, log: Path) -> None:
    with patch("sys.stdin") as stdin:
        stdin.buffer = iter(line.encode() for line in lane_output.splitlines(keepends=True))
        tee_main([str(log)])


@pytest.mark.parametrize("backend", _LANES)
def test_no_writer_leaks_the_credential_on_any_lane(
    backend: str, artifacts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _drive_every_writer(backend, artifacts, capsys)

    written = {path.name: path.read_text(encoding="utf-8") for path in artifacts.iterdir()}
    leaks = sorted(name for name, text in written.items() if _pieces(text))
    assert leaks == [], f"{backend}: the credential reached {leaks}"
    assert set(written) == {
        "single.html",
        "single.md",
        "s.json",
        "pass_at_k.html",
        "pass_at_k.md",
        "pass_at_k.json",
        "matrix.html",
        "dash.md",
        "merged.json",
        "w.md",
        "eval-run.log",
    }
    assert REDACTED in written["single.html"]
    assert REDACTED in written["pass_at_k.html"]
    assert REDACTED in written["eval-run.log"]


_TAG_HOLDING_THE_MARKER = re.compile(rf"<[^<>]*{re.escape(REDACTED)}[^<>]*>")


def test_a_one_letter_named_credential_leaves_every_artifact_well_formed(
    artifacts: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The eval-heal JSON is machine-read (ci-status, green-proof, merge), so a short
    # local key must never rewrite `false` or a tag name the way a 16-char window would.
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "a")

    _drive_every_writer(API_BACKEND, artifacts, capsys)

    written = {path.name: path.read_text(encoding="utf-8") for path in artifacts.iterdir()}
    for name in ("s.json", "pass_at_k.json", "merged.json"):
        json.loads(written[name])
    html_files = [name for name in written if name.endswith(".html")]
    assert html_files
    assert {name: tags for name in html_files if (tags := _TAG_HOLDING_THE_MARKER.findall(written[name]))} == {}


def _pieces(text: str) -> list[str]:
    return [piece for start in range(len(TOKEN) - _PIECE + 1) if (piece := TOKEN[start : start + _PIECE]) in text]


@pytest.fixture
def shards(artifacts: Path) -> Path:
    result = evaluate(_spec(), eval_run_from_messages(_spec(), _sdk_messages(tool_result_block=True)), judge=_judge)
    RunReportPaths(summary_md=artifacts / "shard.md", summary_json=artifacts / "shard.json").write_single_trial(
        [result]
    )
    return artifacts


_RUN_URL = f"https://ci.invalid/run?t={TOKEN}"
_STDOUT_MERGES: dict[str, Callable[[Path], object]] = {
    "merge-summaries": lambda shards: merge_summaries(
        [str(shards / "shard.md")], run_url=_RUN_URL, sha=_SHA, generated_at="2026-10-05", out=None
    ),
    "merge-summary-json": lambda shards: merge_summary_json(
        [str(shards / "shard.json")], sha=_SHA, generated_at=f"2026-10-05 {TOKEN}", out=None
    ),
    "merge-summaries-script": lambda shards: merge_summaries_script(
        [str(shards / "shard.md"), "--run-url", _RUN_URL, "--sha", _SHA, "--generated-at", "2026-10-05"]
    ),
}


@pytest.mark.parametrize("merge", _STDOUT_MERGES.values(), ids=_STDOUT_MERGES.keys())
def test_a_merge_printed_to_stdout_is_redacted(
    merge: Callable[[Path], object], shards: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()

    merge(shards)

    printed = capsys.readouterr().out
    assert REDACTED in printed
    assert _pieces(printed) == []
