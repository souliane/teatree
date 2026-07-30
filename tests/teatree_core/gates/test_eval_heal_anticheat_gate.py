"""Anti-cheat structural gate for the CI-eval self-healing loop (#3201 PR-2).

The healer must fix the CODE that made a behavioral eval red — never the test.
:func:`assert_fix_touches_only_code` refuses any fix diff that touches the
scenario tree (``evals/scenarios/**``) or the red-matcher grading machinery
(``matchers.py`` / ``triage.py`` / ``judge.py`` / ``matcher_vacuity.py``).

Symmetric corpus: must-BLOCK is a diff touching any forbidden path; must-ALLOW
is a diff touching only product code. Each must-ALLOW is anti-vacuous against a
same-shaped must-BLOCK — the forbidden path is what flips the verdict.
"""

import pytest

from teatree.core.gates.eval_heal_anticheat_gate import (
    EvalHealCheatError,
    assert_fix_touches_only_code,
    classify_fix_diff,
)


class TestClassifyFixDiff:
    def test_pure_code_fix_is_clean(self) -> None:
        # GREEN: the real fix lever — skill prose + core code — is never forbidden.
        assert classify_fix_diff(["skills/rules/SKILL.md", "src/teatree/loop/tick.py"]) == ()

    def test_scenario_tree_is_forbidden(self) -> None:
        # RED-1: editing the test (the scenario yaml) is the canonical cheat.
        assert classify_fix_diff(["evals/scenarios/rules.yaml"]) == ("evals/scenarios/rules.yaml",)

    def test_nested_scenario_file_is_forbidden(self) -> None:
        assert classify_fix_diff(["evals/scenarios/sub/deep.yaml"]) == ("evals/scenarios/sub/deep.yaml",)

    def test_matcher_engine_is_forbidden(self) -> None:
        # RED-2: weakening the matcher that decides pass/fail is a cheat.
        assert classify_fix_diff(["src/teatree/eval/matchers.py"]) == ("src/teatree/eval/matchers.py",)

    def test_triage_classifier_is_forbidden(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/triage.py"]) == ("src/teatree/eval/triage.py",)

    def test_judge_is_forbidden(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/judge.py"]) == ("src/teatree/eval/judge.py",)

    def test_vacuity_guard_is_forbidden(self) -> None:
        # Neutering the anti-vacuity guard is a way to suppress a red.
        assert classify_fix_diff(["src/teatree/eval/matcher_vacuity.py"]) == ("src/teatree/eval/matcher_vacuity.py",)

    def test_other_eval_runner_code_is_allowed(self) -> None:
        # ANTI-VACUOUS: a sibling eval file that is NOT grading machinery is fine —
        # the ban is surgical, not "all of src/teatree/eval".
        assert classify_fix_diff(["src/teatree/eval/report.py"]) == ()

    def test_leading_dot_slash_is_normalized(self) -> None:
        assert classify_fix_diff(["./evals/scenarios/rules.yaml"]) == ("./evals/scenarios/rules.yaml",)

    def test_mixed_diff_returns_only_the_forbidden_paths(self) -> None:
        touched = classify_fix_diff(
            ["src/teatree/loop/tick.py", "evals/scenarios/rules.yaml", "src/teatree/eval/judge.py"]
        )
        assert touched == ("evals/scenarios/rules.yaml", "src/teatree/eval/judge.py")

    def test_substring_lookalike_is_not_forbidden(self) -> None:
        # A path that merely CONTAINS "matchers" is not the grader file itself.
        assert classify_fix_diff(["src/teatree/eval/matchers_helpers.py"]) == ()
        assert classify_fix_diff(["docs/evals/scenarios-guide.md"]) == ()


class TestAssertFixTouchesOnlyCode:
    def test_clean_diff_does_not_raise(self) -> None:
        assert_fix_touches_only_code(["src/teatree/loop/tick.py"])  # does not raise

    def test_forbidden_diff_raises(self) -> None:
        with pytest.raises(EvalHealCheatError):
            assert_fix_touches_only_code(["evals/scenarios/rules.yaml"])

    def test_error_names_every_forbidden_path(self) -> None:
        with pytest.raises(EvalHealCheatError) as exc:
            assert_fix_touches_only_code(["evals/scenarios/rules.yaml", "src/teatree/eval/matchers.py"])
        message = str(exc.value)
        assert "evals/scenarios/rules.yaml" in message
        assert "src/teatree/eval/matchers.py" in message

    def test_error_explains_the_fix_the_code_not_the_test_rule(self) -> None:
        with pytest.raises(EvalHealCheatError) as exc:
            assert_fix_touches_only_code(["evals/scenarios/rules.yaml"])
        assert "code" in str(exc.value).lower()
