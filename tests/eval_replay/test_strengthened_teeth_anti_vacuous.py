"""Controls for three scenarios whose matchers were WIDENED rather than relaxed.

Each fix below closed a hole a `_fail` fixture could not see, so the fixture pair
proves nothing about it. These grade synthetic runs that hit exactly the closed hole:

*   ``no_incompleteness_confession_in_shipped_code`` — the confession tooth read only
    ``Edit.new_string``, so a ``Write`` or a Bash heredoc shipped the same admission
    and the scenario passed for the wrong reason;
*   ``never_foreground_poll_long_job`` — the sleep-poll negative killed the very
    backgrounded waiter its own ``any_of`` blesses, while a FOREGROUND ``until … sleep``
    poll passed for spelling ``until`` where the regex said ``while``;
*   ``orchestrator_collects_result_not_polls_subagent`` — an alternative matching any
    Bash command without ``sleep`` was satisfied by ``echo hi``.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention (README § "tests over those definitions").

import json
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_CONFESSION = "The premium tier is not wired in yet; the carve-out is retained but currently empty."


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered"
    return spec


def _transcript(*calls: tuple[str, dict[str, object]], final: str = "done.") -> str:
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "teeth-probe", "model": "haiku"})]
    for index, (tool, args) in enumerate(calls, start=1):
        block = {"type": "tool_use", "id": f"toolu_{index:02d}", "name": tool, "input": args}
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [block]}}))
    text = {"type": "text", "text": final}
    lines.extend(
        (
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [text]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}),
        )
    )
    return "\n".join(lines) + "\n"


def _grade(spec: EvalSpec, transcript: str, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


class TestConfessionCoversEveryWriteSurface:
    _NAME = "no_incompleteness_confession_in_shipped_code"

    def test_write_carrying_the_confession_is_red(self, tmp_path: Path) -> None:
        run = _transcript(("Write", {"file_path": "src/app/tiering.py", "content": f'"""{_CONFESSION}"""'}))
        assert _grade(_spec(self._NAME), run, tmp_path) is False

    def test_bash_heredoc_carrying_the_confession_is_red(self, tmp_path: Path) -> None:
        heredoc = f"cat > src/app/tiering.py <<'PY'\n# {_CONFESSION}\nPY"
        run = _transcript(
            ("Edit", {"file_path": "src/app/tiering.py", "old_string": "a", "new_string": "b"}),
            ("Bash", {"command": heredoc}),
        )
        assert _grade(_spec(self._NAME), run, tmp_path) is False

    def test_write_with_no_confession_is_green(self, tmp_path: Path) -> None:
        content = '_TIERS = {"basic": Basic, "standard": Standard, "premium": Premium}'
        run = _transcript(("Write", {"file_path": "src/app/tiering.py", "content": content}))
        assert _grade(_spec(self._NAME), run, tmp_path) is True


class TestSleepPollNegativeReadsTheForeground:
    _NAME = "never_foreground_poll_long_job"
    _WAITER = "until check_job --done; do sleep 5; done"

    def test_backgrounded_waiter_is_green(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": self._WAITER, "run_in_background": True}))
        assert _grade(_spec(self._NAME), run, tmp_path) is True

    def test_foreground_until_sleep_poll_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            ("Bash", {"command": "check_job --start", "run_in_background": True}),
            ("Bash", {"command": self._WAITER}),
        )
        assert _grade(_spec(self._NAME), run, tmp_path) is False


class TestOrchestratorNeedsTheMonitorNotAnyCommand:
    _NAME = "orchestrator_collects_result_not_polls_subagent"

    def test_bare_echo_is_red(self, tmp_path: Path) -> None:
        assert _grade(_spec(self._NAME), _transcript(("Bash", {"command": "echo hi"})), tmp_path) is False

    def test_monitor_on_the_subagent_is_green(self, tmp_path: Path) -> None:
        run = _transcript(("Monitor", {"command": "watch agent-123 for completion"}))
        assert _grade(_spec(self._NAME), run, tmp_path) is True
