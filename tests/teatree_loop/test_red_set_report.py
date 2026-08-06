"""The cross-PR red-set verdict, render, and signature (#4090).

Pure logic over an already-gathered set, so these are unit tests by the
`AGENTS.md` carve-out. Each verdict rung gets the case that ONLY it explains:
the cycle the per-PR views cannot see, the shared cause that is one fix, the
inherited red that reframes the whole board, and the two indeterminate rungs
that must refuse the cycle claim rather than guess it.
"""

from teatree.loop.red_set_report import PrRedRecord, RedSetReport, SetVerdict, analyse_red_set

SLUG = "souliane/teatree"


def _record(pr_id: int, *failing: str, base_current: bool = True) -> PrRedRecord:
    return PrRedRecord(
        ref=f"{SLUG}#{pr_id}",
        failing=frozenset(failing),
        base_current=base_current,
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
    )


def _analyse(*records: PrRedRecord, main_failing: frozenset[str] | None = frozenset()) -> RedSetReport | None:
    return analyse_red_set(slug=SLUG, main_failing=main_failing, records=records)


class TestVerdict:
    def test_disjoint_failing_sets_on_a_current_base_are_disjoint_reds(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))

        assert report is not None
        assert report.verdict is SetVerdict.DISJOINT_REDS

    def test_overlapping_failing_sets_are_one_shared_cause(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-a", "shard-b"))

        assert report is not None
        assert report.verdict is SetVerdict.SHARED_CAUSE
        assert report.shared == frozenset({"shard-a"})

    def test_main_failing_a_check_the_set_fails_reframes_the_board(self) -> None:
        report = _analyse(
            _record(4101, "shard-a"),
            _record(4102, "shard-b"),
            main_failing=frozenset({"shard-a"}),
        )

        assert report is not None
        assert report.verdict is SetVerdict.MAIN_RED
        assert report.inherited == frozenset({"shard-a"})

    def test_main_red_on_an_unrelated_check_does_not_reframe_the_board(self) -> None:
        # A red on main that no open PR is failing blocks nothing — the set analysis
        # still runs, and main's own failure is reported rather than acted on.
        report = _analyse(
            _record(4101, "shard-a"),
            _record(4102, "shard-b"),
            main_failing=frozenset({"docs"}),
        )

        assert report is not None
        assert report.verdict is SetVerdict.DISJOINT_REDS
        assert report.inherited == frozenset()

    def test_a_stale_base_run_refuses_the_cycle_claim(self) -> None:
        # A red judged against a base the branch has fallen behind is an UNKNOWN
        # verdict (#4063), so the set is not provably unsatisfiable.
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b", base_current=False))

        assert report is not None
        assert report.verdict is SetVerdict.INDEPENDENT

    def test_unreadable_main_refuses_the_cycle_claim(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"), main_failing=None)

        assert report is not None
        assert report.verdict is SetVerdict.MAIN_INDETERMINATE

    def test_a_lone_red_pr_is_independent(self) -> None:
        report = _analyse(_record(4101, "shard-a"))

        assert report is not None
        assert report.verdict is SetVerdict.INDEPENDENT

    def test_no_red_prs_produce_no_report(self) -> None:
        assert _analyse() is None

    def test_a_pr_with_no_failing_checks_is_not_part_of_the_red_set(self) -> None:
        assert _analyse(_record(4101)) is None


class TestSetArithmetic:
    def test_exclusive_names_are_those_no_other_pr_fails(self) -> None:
        report = _analyse(_record(4101, "shard-a", "docs"), _record(4102, "shard-b", "docs"))

        assert report is not None
        assert dict(report.exclusive()) == {
            f"{SLUG}#4101": frozenset({"shard-a"}),
            f"{SLUG}#4102": frozenset({"shard-b"}),
        }
        assert report.shared == frozenset({"docs"})

    def test_records_are_ordered_by_ref_regardless_of_input_order(self) -> None:
        report = _analyse(_record(4102, "shard-b"), _record(4101, "shard-a"))

        assert report is not None
        assert [record.ref for record in report.records] == [f"{SLUG}#4101", f"{SLUG}#4102"]


class TestRender:
    def test_main_verdict_is_stated_first(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))

        assert report is not None
        assert report.render().splitlines()[0].startswith("main ")

    def test_render_carries_the_verdict_every_pr_and_the_exit(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))

        assert report is not None
        rendered = report.render()
        assert "disjoint-reds" in rendered
        assert f"https://github.com/{SLUG}/pull/4101" in rendered
        assert f"https://github.com/{SLUG}/pull/4102" in rendered
        assert "shard-a" in rendered
        assert "shard-b" in rendered

    def test_disjoint_reds_state_the_observation_not_a_conclusion(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))

        assert report is not None
        rendered = report.render()
        assert "disjoint-reds" in rendered
        assert "no failing check in common" in rendered
        assert "no dependency evidence" in rendered
        assert "cherry-pick" not in rendered
        assert "no merge ordering exists" not in rendered

    def test_the_note_names_both_hypotheses_and_picks_neither(self) -> None:
        # A mutual block and two wholly unrelated reds reach this rung through
        # byte-identical records, so the note must carry both readings.
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))

        assert report is not None
        rendered = report.render()
        assert "mutual block" in rendered
        assert "unrelated failures" in rendered
        assert "either way" in rendered

    def test_main_red_render_names_the_inherited_check(self) -> None:
        report = _analyse(
            _record(4101, "shard-a"),
            _record(4102, "shard-a"),
            main_failing=frozenset({"shard-a"}),
        )

        assert report is not None
        assert "shard-a" in report.render().splitlines()[0]

    def test_stale_base_is_visible_per_pr(self) -> None:
        report = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b", base_current=False))

        assert report is not None
        assert "stale" in report.render()


class TestSignature:
    def test_the_same_set_signs_identically_whatever_the_input_order(self) -> None:
        first = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))
        second = _analyse(_record(4102, "shard-b"), _record(4101, "shard-a"))

        assert first is not None
        assert second is not None
        assert first.signature() == second.signature()

    def test_a_changed_failing_set_is_a_new_claim(self) -> None:
        first = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))
        second = _analyse(_record(4101, "shard-a"), _record(4102, "shard-c"))

        assert first is not None
        assert second is not None
        assert first.signature() != second.signature()

    def test_an_incidental_main_red_outside_the_set_is_the_same_claim(self) -> None:
        # Measured: a FIXED red set took 4 distinct signatures across 8 main commits
        # while `deploy` / `refresh-durations` / `test-shard (3.13, 5)` flipped. The
        # key is the CLAIM, so an incidental main red no PR is failing is not one.
        first = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"), main_failing=frozenset())
        second = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"), main_failing=frozenset({"deploy"}))

        assert first is not None
        assert second is not None
        assert first.signature() == second.signature()

    def test_a_changed_inherited_set_is_a_new_claim(self) -> None:
        first = _analyse(_record(4101, "a", "b"), _record(4102, "a", "b"), main_failing=frozenset({"a"}))
        second = _analyse(_record(4101, "a", "b"), _record(4102, "a", "b"), main_failing=frozenset({"a", "b"}))

        assert first is not None
        assert second is not None
        assert first.signature() != second.signature()

    def test_a_dropped_pr_is_a_new_claim(self) -> None:
        first = _analyse(_record(4101, "shard-a"), _record(4102, "shard-b"))
        second = _analyse(_record(4101, "shard-a"))

        assert first is not None
        assert second is not None
        assert first.signature() != second.signature()
