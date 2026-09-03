"""Anti-cheat structural gate for the CI-eval self-healing loop (#3201 PR-2, #4220).

The healer must fix the CODE that made a behavioral eval red — never the test.
:func:`assert_fix_touches_only_code` refuses any fix diff that touches the
scenario tree (``evals/scenarios/**``) or the eval harness
(``src/teatree/eval/**``), which is default-deny since #4220: a hand-listed set
of four graders admitted ``report.py`` — the module that computes the verdict —
and every other grading module by omission.

Symmetric corpus: must-BLOCK is a diff touching any forbidden path; must-ALLOW
is a diff touching only product code. Each must-ALLOW is anti-vacuous against a
same-shaped must-BLOCK — the forbidden path is what flips the verdict.
:class:`TestGradingSurfaceCoverage` is the conformance pin: the denylist is
checked against the COMPUTED grading call graph, so it cannot drift away from it
again.
"""

from pathlib import Path

import pytest

import teatree.eval
from teatree.core.gates.eval_heal_anticheat_gate import (
    EVAL_HARNESS_ALLOWED_PATHS,
    EvalHealCheatError,
    assert_fix_touches_only_code,
    classify_fix_diff,
)
from teatree.quality.eval_grading_surface import grading_surface


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

    def test_verdict_module_is_forbidden(self) -> None:
        # #4220's headline hole: report.py computes `ScenarioResult.passed`, and the
        # hand-listed denylist omitted it — a fixer could widen the verdict itself.
        assert classify_fix_diff(["src/teatree/eval/report.py"]) == ("src/teatree/eval/report.py",)

    def test_expect_loader_is_forbidden(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/loader.py"]) == ("src/teatree/eval/loader.py",)

    def test_skip_guard_is_forbidden(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/skip_guard.py"]) == ("src/teatree/eval/skip_guard.py",)

    def test_summary_json_is_forbidden(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/summary_json.py"]) == ("src/teatree/eval/summary_json.py",)

    def test_green_proof_is_forbidden(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/green_proof.py"]) == ("src/teatree/eval/green_proof.py",)

    def test_a_new_eval_module_is_denied_by_default(self) -> None:
        # The point of the inversion: a module on no list is refused, so a grader
        # added tomorrow is covered the day it lands, not the day someone lists it.
        assert classify_fix_diff(["src/teatree/eval/brand_new_grader.py"]) == ("src/teatree/eval/brand_new_grader.py",)

    def test_nested_eval_module_is_denied(self) -> None:
        assert classify_fix_diff(["src/teatree/eval/sub/deep.py"]) == ("src/teatree/eval/sub/deep.py",)

    def test_leading_dot_slash_is_normalized(self) -> None:
        assert classify_fix_diff(["./evals/scenarios/rules.yaml"]) == ("./evals/scenarios/rules.yaml",)
        assert classify_fix_diff(["./src/teatree/eval/report.py"]) == ("./src/teatree/eval/report.py",)

    def test_mixed_diff_returns_only_the_forbidden_paths(self) -> None:
        touched = classify_fix_diff(
            ["src/teatree/loop/tick.py", "evals/scenarios/rules.yaml", "src/teatree/eval/judge.py"]
        )
        assert touched == ("evals/scenarios/rules.yaml", "src/teatree/eval/judge.py")

    def test_both_prefixes_are_directory_anchored(self) -> None:
        # ANTI-VACUOUS: the ban is a directory prefix, not a substring — a sibling
        # whose name merely STARTS with a guarded one stays a legal fix target, so
        # every must-BLOCK above is decided by location rather than by spelling.
        assert classify_fix_diff(["src/teatree/evaluation/report.py"]) == ()
        assert classify_fix_diff(["src/teatree/eval_replay_helper.py"]) == ()
        assert classify_fix_diff(["evals/scenarios_archive/old.yaml"]) == ()
        assert classify_fix_diff(["docs/evals/scenarios-guide.md"]) == ()


class TestVendoredCopyIsCoveredToo:
    """A repo that VENDORS this package reports the very same files with a prefix.

    Anchoring the match at the repo root left the healer free to rewrite the test
    in exactly the layout the gate has to defend hardest — a fork where the
    scenarios and the graders are one subdirectory down.
    """

    def test_prefixed_scenario_tree_is_forbidden(self) -> None:
        path = "vendor/pkg/evals/scenarios/rules.yaml"
        assert classify_fix_diff([path]) == (path,)

    def test_prefixed_red_matcher_is_forbidden(self) -> None:
        path = "vendor/pkg/src/teatree/eval/matchers.py"
        assert classify_fix_diff([path]) == (path,)

    def test_prefixed_product_code_is_still_allowed(self) -> None:
        # ANTI-VACUOUS: the prefix alone forbids nothing — only the suffix does.
        assert classify_fix_diff(["vendor/pkg/src/teatree/loop/tick.py"]) == ()

    def test_a_lookalike_suffix_is_not_forbidden(self) -> None:
        assert classify_fix_diff(["docs/my_evals/scenarios-guide.md"]) == ()
        assert classify_fix_diff(["tools/eval/matchers.py"]) == ()


class TestAssertFixTouchesOnlyCode:
    def test_clean_diff_does_not_raise(self) -> None:
        assert_fix_touches_only_code(["src/teatree/loop/tick.py"])  # does not raise

    def test_forbidden_diff_raises(self) -> None:
        with pytest.raises(EvalHealCheatError):
            assert_fix_touches_only_code(["evals/scenarios/rules.yaml"])

    def test_error_names_every_forbidden_path(self) -> None:
        with pytest.raises(EvalHealCheatError) as exc:
            assert_fix_touches_only_code(["evals/scenarios/rules.yaml", "src/teatree/eval/report.py"])
        message = str(exc.value)
        assert "evals/scenarios/rules.yaml" in message
        assert "src/teatree/eval/report.py" in message

    def test_error_explains_the_fix_the_code_not_the_test_rule(self) -> None:
        with pytest.raises(EvalHealCheatError) as exc:
            assert_fix_touches_only_code(["evals/scenarios/rules.yaml"])
        assert "code" in str(exc.value).lower()


class TestGradingSurfaceCoverage:
    """The denylist is checked against the computed grading call graph (#4220).

    Control for the class: restore the pre-#4220 four-element denylist and every
    assertion here goes RED, naming ``report.py`` and the rest of its graph.
    """

    @staticmethod
    def _surface() -> tuple[frozenset[Path], Path]:
        package_dir = Path(teatree.eval.__file__).parent
        return grading_surface(package_dir), package_dir

    @staticmethod
    def _gate_path(module: Path, package_dir: Path) -> str:
        return f"src/teatree/eval/{module.relative_to(package_dir).as_posix()}"

    def test_every_module_on_the_grading_call_graph_is_denied(self) -> None:
        surface, package_dir = self._surface()
        paths = [self._gate_path(module, package_dir) for module in sorted(surface)]
        assert classify_fix_diff(paths) == tuple(paths)

    def test_the_surface_is_not_a_single_module(self) -> None:
        surface, _ = self._surface()
        assert len(surface) > 1

    def test_no_allowlist_entry_sits_on_the_grading_call_graph(self) -> None:
        surface, package_dir = self._surface()
        graded = {self._gate_path(module, package_dir) for module in surface}
        assert EVAL_HARNESS_ALLOWED_PATHS & graded == frozenset()

    def test_an_allowlisted_path_is_actually_exempted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exemption mechanism is live, not dead config: with an entry present
        # the gate lets that one path through while its siblings stay refused.
        monkeypatch.setattr(
            "teatree.core.gates.eval_heal_anticheat_gate.EVAL_HARNESS_ALLOWED_PATHS",
            frozenset({"src/teatree/eval/summary_markdown.py"}),
        )
        assert classify_fix_diff(["src/teatree/eval/summary_markdown.py"]) == ()
        assert classify_fix_diff(["src/teatree/eval/report.py"]) == ("src/teatree/eval/report.py",)
