"""Grade a captured session against its ground-truth label."""

from teatree.eval.corpus_grade import captured_run, grade
from teatree.eval.corpus_loader import CORPUS_DIR, discover_corpus
from teatree.eval.corpus_models import CorpusLabel
from teatree.eval.report import JudgeOutcome
from teatree.eval.session_transcript import parse_session_jsonl

_BG_SESSION = (
    '{"type":"assistant","message":{"role":"assistant","content":['
    '{"type":"text","text":"backgrounding"},'
    '{"type":"tool_use","id":"t1","name":"Bash",'
    '"input":{"command":"gh run watch","run_in_background":true}}]}}\n'
)
_BLOCKING_SESSION = (
    '{"type":"assistant","message":{"role":"assistant","content":['
    '{"type":"tool_use","id":"t1","name":"Bash",'
    '"input":{"command":"gh run watch"}}]}}\n'
)


def _label(entry_id: str) -> CorpusLabel:
    return next(label for label in discover_corpus() if label.entry_id == entry_id)


class TestCapturedRun:
    def test_builds_eval_run_from_session_events(self) -> None:
        label = _label("background_ci_watch")
        events = parse_session_jsonl(_BG_SESSION)
        run = captured_run(label, events)
        assert run.spec_name == "background_ci_watch"
        assert [c.name for c in run.tool_calls] == ["Bash"]
        assert run.tool_calls[0].input["run_in_background"] is True
        assert run.is_error is False
        assert run.terminal_reason == "completed"

    def test_carries_text_blocks(self) -> None:
        label = _label("background_ci_watch")
        run = captured_run(label, parse_session_jsonl(_BG_SESSION))
        assert any("backgrounding" in block for block in run.text_blocks)


class TestGrade:
    def test_passes_a_compliant_session(self) -> None:
        label = _label("background_ci_watch")
        result = grade(label, parse_session_jsonl(_BG_SESSION))
        assert result.passed is True

    def test_fails_a_mutated_session(self) -> None:
        label = _label("background_ci_watch")
        result = grade(label, parse_session_jsonl(_BLOCKING_SESSION))
        assert result.passed is False

    def test_grades_the_shipped_capture_green(self) -> None:
        label = _label("background_ci_watch")
        text = (CORPUS_DIR / f"{label.entry_id}.session.jsonl").read_text(encoding="utf-8")
        result = grade(label, parse_session_jsonl(text))
        assert result.passed is True

    def test_judge_oracle_uses_injected_grader(self) -> None:
        label = _label("faithful_explanation")
        session = (
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"text","text":"I renamed compute to compute_total."}]}}\n'
        )
        calls: list[str] = []

        def _judge(spec: object, run: object) -> JudgeOutcome:
            calls.append("graded")
            return JudgeOutcome(passed=True, skipped=False, rationale="faithful")

        result = grade(label, parse_session_jsonl(session), judge=_judge)
        assert calls == ["graded"]
        assert result.passed is True
        assert result.judge is not None
        assert result.judge.rationale == "faithful"

    def test_judge_oracle_fails_when_grader_fails(self) -> None:
        label = _label("faithful_explanation")
        session = '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"x"}]}}\n'

        def _judge(spec: object, run: object) -> JudgeOutcome:
            return JudgeOutcome(passed=False, skipped=False, rationale="hallucinated a change")

        result = grade(label, parse_session_jsonl(session), judge=_judge)
        assert result.passed is False

    def test_dirty_session_marks_error(self) -> None:
        label = _label("background_ci_watch")
        dirty = (
            '{"type":"assistant","message":{"role":"assistant","stop_reason":"max_tokens","content":['
            '{"type":"tool_use","id":"t1","name":"Bash",'
            '"input":{"command":"gh run watch","run_in_background":true}}]}}\n'
        )
        run = captured_run(label, parse_session_jsonl(dirty))
        assert run.is_error is True
        assert run.terminal_reason == "max_tokens"


class TestCapturedRunEdgeCases:
    def test_no_assistant_event_aborts(self) -> None:
        label = _label("background_ci_watch")
        run = captured_run(label, parse_session_jsonl('{"type":"user","message":{"content":"hi"}}\n'))
        assert run.terminal_reason == "aborted"
        assert run.is_error is True
        assert run.tool_calls == ()
        assert run.text_blocks == ()

    def test_non_dict_message_is_incomplete_not_clean_completion(self) -> None:
        # A malformed (non-dict) final assistant message cannot confirm completion —
        # it grades as an error, never a silent clean pass (a truncated/corrupt
        # capture must not read green).
        label = _label("background_ci_watch")
        run = captured_run(label, parse_session_jsonl('{"type":"assistant","message":"oops"}\n'))
        assert run.terminal_reason == "incomplete"
        assert run.is_error is True
        assert run.text_blocks == ()

    def test_non_text_content_block_is_dropped_from_text(self) -> None:
        label = _label("background_ci_watch")
        session = (
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"thinking","text":"private chain"},'
            '{"type":"text","text":"public answer"}]}}\n'
        )
        run = captured_run(label, parse_session_jsonl(session))
        assert run.text_blocks == ("public answer",)

    def test_text_block_with_non_string_text_is_dropped(self) -> None:
        label = _label("background_ci_watch")
        session = (
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"text","text":42},{"type":"text"},"raw string block"]}}\n'
        )
        run = captured_run(label, parse_session_jsonl(session))
        assert run.text_blocks == ()
