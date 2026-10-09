"""Every reader handles the command-count matcher added to the eval union."""

from pathlib import Path

from scripts.eval.run_against_fixture import _describe
from teatree.eval.loader import _parse_matcher
from teatree.eval.models import EvalSpec, ToolCallCountMatcher
from teatree.eval.persistence import _matcher_detail
from teatree.eval.report import MatcherResult
from teatree.loops.dream.llm_eval_proposer import _matchers_to_mappings


def _matcher() -> ToolCallCountMatcher:
    return ToolCallCountMatcher("Bash", "command", "git push", 1, ("src/app.py",))


def test_count_matcher_persists_as_matcher_detail() -> None:
    detail = _matcher_detail(MatcherResult(matcher=_matcher(), passed=True, message=""))
    assert detail == {
        "kind": "tool_call_count",
        "tool": "Bash",
        "arg_path": "command",
        "operator": "~",
        "value": "git push",
        "passed": True,
    }


def test_count_matcher_round_trips_through_proposal_mapping() -> None:
    spec = EvalSpec("push-count", "count pushes", "skills/code/SKILL.md", "push", (_matcher(),), Path("spec.yaml"))
    mapping = _matchers_to_mappings(spec)[0]
    assert _parse_matcher(mapping, spec.name, spec.source_path) == _matcher()


def test_count_matcher_has_fixture_failure_description() -> None:
    assert _describe(_matcher()) == "tool_call_count Bash.command ~ 'git push' == 1"
