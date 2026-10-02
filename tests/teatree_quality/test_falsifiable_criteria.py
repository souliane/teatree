"""Acceptance criteria must be falsifiable — no criterion satisfiable by inaction (#3762).

The root cause of a shipped change whose implementation phase was silently
skipped: its acceptance criterion was "the existing resolver test suite passes
UNMODIFIED", satisfied by stating that the resolver was never touched. A suite
trivially passes unmodified when the module is never modified — the criterion
had no state of the world that made it FAIL, so it certified the skip as a
success.

The repo already proves every regression test RED before the fix; these pin the
same anti-vacuity rule one level up, at feature-level acceptance criteria.
"""

from teatree.quality.falsifiable_criteria import (
    absence_satisfied_criteria,
    falsifiability_violation,
    is_absence_satisfied,
)

_SKIPPED_PHASE_CRITERION = "the existing resolver test suite passes UNMODIFIED"


class TestAbsenceSatisfiedDetection:
    def test_flags_the_skipped_phase_criterion(self) -> None:
        assert is_absence_satisfied(_SKIPPED_PHASE_CRITERION)

    def test_flags_the_stays_unchanged_family(self) -> None:
        for text in (
            "resolution.py stays unchanged",
            "The public API remains the same",
            "no changes to the CLI surface",
            "the settings module is not modified",
            "existing tests continue to pass without modification",
            "nothing in the loop is touched",
            "no regression in the sweep scanner",
            "the schema does not change",
        ):
            assert is_absence_satisfied(text), text

    def test_does_not_flag_a_positive_criterion(self) -> None:
        for text in (
            "the resolver reads defaults.toml and a value changed there changes the resolved setting",
            "`t3 tool push-gate` exits non-zero on an unpegged deferred import",
            "a merge_safe verdict on a phase-less ticket is refused",
            "the dashboard renders one provenance row per task",
        ):
            assert not is_absence_satisfied(text), text

    def test_a_positive_criterion_naming_an_unchanged_input_is_not_flagged(self) -> None:
        # "unchanged" describes the INPUT here; the criterion still fails when the
        # feature is absent, so it is falsifiable.
        text = "re-running the resolver against an unchanged config returns the cached value without a disk read"
        assert not is_absence_satisfied(text)

    def test_enumerates_every_offending_criterion_with_its_ordinal(self) -> None:
        offenders = absence_satisfied_criteria(
            ["the parser accepts a nested table", _SKIPPED_PHASE_CRITERION, "the CLI surface is unchanged"]
        )
        assert [ordinal for ordinal, _ in offenders] == [2, 3]


class TestFalsifiabilityViolation:
    def test_an_absence_only_rubric_is_refused(self) -> None:
        violation = falsifiability_violation([_SKIPPED_PHASE_CRITERION])
        assert "satisfiable by INACTION" in violation
        assert _SKIPPED_PHASE_CRITERION in violation

    def test_every_criterion_absence_satisfied_is_refused(self) -> None:
        assert falsifiability_violation(["resolution.py stays unchanged", "no changes to the CLI"])

    def test_an_absence_criterion_paired_with_a_positive_one_is_accepted(self) -> None:
        assert not falsifiability_violation(
            [
                "the resolver reads defaults.toml, so editing a value there changes the resolved setting",
                _SKIPPED_PHASE_CRITERION,
            ]
        )

    def test_an_all_positive_rubric_is_accepted(self) -> None:
        assert not falsifiability_violation(["the resolver reads defaults.toml", "`t3 info` prints the source file"])

    def test_an_empty_list_has_no_falsifiability_violation(self) -> None:
        # Emptiness is the sibling vacuity, refused by ``Rubric.populate`` itself.
        assert not falsifiability_violation([])
