"""The deterministic architecture pre-check validator (PR-11).

The removability / harness-vs-data check is the tenth check this PR adds; the
anti-vacuity pair is: it FIRES on a fixture whose checks 1-9 are answered but
that carries no removability answer, and it is SILENT on a fully-answered
fixture.
"""

from teatree.quality.architecture_precheck import (
    REMOVABILITY_CHECK,
    REQUIRED_CHECKS,
    inverted_test_findings,
    is_answered,
    parse_sections,
    precheck_findings,
    removability_answered,
)

_ANSWERED_1_TO_9 = """## Architecture pre-check — teatree#2743

## 1. BLUEPRINT § alignment
§5.2 phase dispatch — per-phase least-privilege on Lane A.

## 2. FSM phase boundaries
n/a — no transition.

## 3. Extension-point contracts
None — reuses the phase_tools SSOT.

## 4. Component boundaries
agents/ owns the Lane-A boundary map.

## 5. Dependency direction
agents -> core.modelkit, no backwards edge; tach green.

## 6. Test surface
test_sdk_tool_map.py asserts reviewing denies Bash.

## 7. Resilience invariants
n/a — no external write.

## 8. Identity and key normalization
capability names canonical; normalize_phase at the boundary.

## 9. Behavior preservation / capability deletion
n/a — purely additive; write phases unchanged.
"""

_REMOVABILITY_SECTION = """
## 10. Removability / harness-vs-data
Removable — deleting the map reverts to today's single-disallow list. Lives in
the harness (a dispatch-time tool policy), not in data/config.
"""

_COMPLETE = _ANSWERED_1_TO_9 + _REMOVABILITY_SECTION


class TestRemovabilityCheckFires:
    def test_fires_when_removability_answer_absent(self) -> None:
        findings = precheck_findings(_ANSWERED_1_TO_9)
        assert any(str(REMOVABILITY_CHECK.number) in f and "Removability" in f for f in findings), findings

    def test_silent_on_complete_precheck(self) -> None:
        assert precheck_findings(_COMPLETE) == []

    def test_removability_answered_predicate(self) -> None:
        assert removability_answered(_COMPLETE) is True
        assert removability_answered(_ANSWERED_1_TO_9) is False

    def test_new_check_is_the_only_finding_when_1_to_9_answered(self) -> None:
        # Proves the removability finding is what fires, not a pre-existing gap.
        findings = precheck_findings(_ANSWERED_1_TO_9)
        assert findings == [f"check {REMOVABILITY_CHECK.number} ({REMOVABILITY_CHECK.title}) is unanswered"]


class TestPlaceholderIsUnanswered:
    def test_bare_placeholder_section_counts_as_unanswered(self) -> None:
        placeholder = _ANSWERED_1_TO_9 + "\n## 10. Removability / harness-vs-data\n<removable? harness or data?>\n"
        assert removability_answered(placeholder) is False
        assert any(str(REMOVABILITY_CHECK.number) in f for f in precheck_findings(placeholder))

    def test_empty_section_counts_as_unanswered(self) -> None:
        assert is_answered("") is False
        assert is_answered("   \n\n") is False
        assert is_answered("<placeholder>") is False

    def test_real_answer_counts_as_answered(self) -> None:
        assert is_answered("Removable; lives in the harness.") is True
        assert is_answered("n/a — purely additive") is True


class TestVacuousOnEmpty:
    def test_empty_text_yields_no_findings(self) -> None:
        assert precheck_findings("") == []
        assert precheck_findings("   \n\t\n") == []


class TestParseSections:
    def test_a_following_non_numbered_heading_closes_the_section(self) -> None:
        text = "## 10. Removability / harness-vs-data\n<removable?>\n\n## Workflow\nHand off to code.\n"
        sections = parse_sections(text)
        # Section 10's body must NOT swallow the trailing "## Workflow" prose,
        # else a bare-placeholder section would look answered.
        assert sections[10].strip() == "<removable?>"

    def test_duplicate_number_keeps_the_last(self) -> None:
        text = "## 1. BLUEPRINT § alignment\n<todo>\n## 1. BLUEPRINT § alignment\nfilled in\n"
        assert parse_sections(text)[1] == "filled in"


class TestRequiredChecksShape:
    def test_ten_checks_removability_is_last(self) -> None:
        assert len(REQUIRED_CHECKS) == 10
        assert REQUIRED_CHECKS[-1] is REMOVABILITY_CHECK
        assert [c.number for c in REQUIRED_CHECKS] == list(range(1, 11))


class TestInvertedTestFindings:
    """Check #9's named anti-pattern (#2663 dream-batch dea750a552f8f2d2).

    `returncode == 1` inverted to `== 0`.
    """

    def test_an_inverted_must_block_assertion_is_flagged(self) -> None:
        diff = (
            "diff --git a/tests/test_gate.py b/tests/test_gate.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/tests/test_gate.py\n"
            "+++ b/tests/test_gate.py\n"
            "@@ -10,7 +10,7 @@ def test_gate_blocks_bad_input():\n"
            "     result = run_gate(bad_input)\n"
            "-    assert result.returncode == 1\n"
            "+    assert result.returncode == 0\n"
        )
        findings = inverted_test_findings(diff)
        assert len(findings) == 1
        assert "tests/test_gate.py" in findings[0]
        assert "== 1" in findings[0]
        assert "== 0" in findings[0]

    def test_a_brand_new_test_asserting_zero_is_not_flagged(self) -> None:
        """No prior `== 1` history — new coverage, not an inversion."""
        diff = (
            "diff --git a/tests/test_gate.py b/tests/test_gate.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/tests/test_gate.py\n"
            "+++ b/tests/test_gate.py\n"
            "@@ -10,3 +10,6 @@ def test_gate_blocks_bad_input():\n"
            "     result = run_gate(bad_input)\n"
            "     assert result.returncode == 1\n"
            "+def test_gate_allows_good_input():\n"
            "+    result = run_gate(good_input)\n"
            "+    assert result.returncode == 0\n"
        )
        assert inverted_test_findings(diff) == []

    def test_an_unrelated_line_change_is_not_flagged(self) -> None:
        diff = (
            "diff --git a/tests/test_gate.py b/tests/test_gate.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/tests/test_gate.py\n"
            "+++ b/tests/test_gate.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-# old comment\n"
            "+# new comment\n"
            "     pass\n"
        )
        assert inverted_test_findings(diff) == []

    def test_a_non_python_file_is_not_scanned(self) -> None:
        diff = (
            "diff --git a/notes.md b/notes.md\n"
            "index 1111111..2222222 100644\n"
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -1,2 +1,2 @@\n"
            "-returncode == 1\n"
            "+returncode == 0\n"
        )
        assert inverted_test_findings(diff) == []

    def test_empty_diff_yields_no_findings(self) -> None:
        assert inverted_test_findings("") == []
