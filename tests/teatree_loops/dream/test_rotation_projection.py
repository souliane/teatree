"""The deferral report says whether the rotation CONVERGES (#4671 ask 4).

A pass deferring 91% of its snippets every night reads as a queue that never drains. It
is in fact a rotation: the cursor advances each pass and the corpus is swept in a bounded
number of them. The percentage alone cannot tell those apart, so the sweep length rides
the same report.
"""

from teatree.loops.dream.distill import BatchDistillOutcome


class TestSweepProjection:
    def test_sweep_length_rounds_up_from_the_per_pass_advance(self) -> None:
        outcome = BatchDistillOutcome(clusters=[], empty_batches=0, rotation_len=207, rotation_advance=17)
        assert outcome.sweep_passes == 13  # 207/17 = 12.2 → a 13th pass closes the sweep

    def test_an_exact_division_needs_no_extra_pass(self) -> None:
        outcome = BatchDistillOutcome(clusters=[], empty_batches=0, rotation_len=200, rotation_advance=20)
        assert outcome.sweep_passes == 10

    def test_a_cursor_that_cannot_advance_reports_no_sweep(self) -> None:
        # A pass whose batches all failed proposes no advance, so the rotation is parked —
        # the report must NOT claim a finite sweep it cannot deliver.
        outcome = BatchDistillOutcome(clusters=[], empty_batches=0, rotation_len=207, rotation_advance=0)
        assert outcome.sweep_passes == 0

    def test_no_rotation_reports_no_sweep(self) -> None:
        assert BatchDistillOutcome(clusters=[], empty_batches=0).sweep_passes == 0
