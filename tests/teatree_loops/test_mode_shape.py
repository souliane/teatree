"""A mode that stops the pipeline draining must not leave it filling (#4096).

The overnight ``maintenance`` window masked ``ship`` and ``tickets`` off but named no
opinion on ``issue_implementer``, so intake inherited ``Loop.enabled`` — which on that box
was ON — and kept claiming issues nothing could merge. Absent means INHERIT, so the base
flag is what decides whether an absent entry actually fills: the rule takes it as an input
rather than assuming the worst, or every legitimate mask would read as broken.
"""

from teatree.loops.mode_shape import DELIVERY_LOOPS, INTAKE_LOOPS, intake_without_delivery

_INTAKE_RUNNING = dict.fromkeys(INTAKE_LOOPS, True)
_INTAKE_PARKED = dict.fromkeys(INTAKE_LOOPS, False)


class TestIntakeWithoutDelivery:
    def test_the_maintenance_shape_is_named(self) -> None:
        found = intake_without_delivery(
            {"tickets": False, "ship": False, "review": False}, base_enabled=_INTAKE_RUNNING
        )

        assert found is not None
        assert found.masked_delivery == ("ship", "tickets")
        assert found.admitted_intake == (
            ("issue_implementer", "no entry, and its Loop row is enabled, so it inherits ON"),
        )

    def test_the_same_mask_is_clean_while_the_inherited_loop_is_off(self) -> None:
        """Absent means inherit — with the base flag off nothing fills, so nothing is wrong."""
        assert intake_without_delivery({"tickets": False, "ship": False}, base_enabled=_INTAKE_PARKED) is None

    def test_an_unknown_base_state_reads_as_not_running(self) -> None:
        assert intake_without_delivery({"ship": False}, base_enabled={}) is None

    def test_masking_the_intake_loop_off_too_is_clean(self) -> None:
        entries = {"ship": False, "tickets": False, "issue_implementer": False}

        assert intake_without_delivery(entries, base_enabled=_INTAKE_RUNNING) is None

    def test_a_mask_that_stops_nothing_draining_is_clean(self) -> None:
        assert intake_without_delivery({"ship": True, "tickets": True}, base_enabled=_INTAKE_RUNNING) is None

    def test_an_empty_mask_is_clean(self) -> None:
        assert intake_without_delivery({}, base_enabled=_INTAKE_RUNNING) is None

    def test_one_masked_delivery_loop_is_enough_to_ask_the_question(self) -> None:
        found = intake_without_delivery({"ship": False}, base_enabled=_INTAKE_RUNNING)

        assert found is not None
        assert found.masked_delivery == ("ship",)

    def test_intake_forced_on_under_masked_delivery_holds_whatever_the_base_flag_says(self) -> None:
        found = intake_without_delivery({"ship": False, "issue_implementer": True}, base_enabled=_INTAKE_PARKED)

        assert found is not None
        assert found.admitted_intake == (("issue_implementer", "forced on by the mask"),)

    def test_a_corrupt_value_reads_as_inherit_not_as_a_mask(self) -> None:
        """``Mode.state_for`` degrades a non-bool to inherit; this must agree with it."""
        entries = {"ship": "false", "issue_implementer": False}

        assert intake_without_delivery(entries, base_enabled=_INTAKE_RUNNING) is None

    def test_the_detail_names_both_halves_and_the_remedy(self) -> None:
        found = intake_without_delivery({"ship": False, "tickets": False}, base_enabled=_INTAKE_RUNNING)

        assert found is not None
        assert "ship" in found.detail
        assert "issue_implementer" in found.detail
        assert "nothing can merge" in found.detail
        assert "t3 loop preset edit" in found.detail

    def test_delivery_and_intake_do_not_overlap(self) -> None:
        assert not set(DELIVERY_LOOPS) & set(INTAKE_LOOPS)
