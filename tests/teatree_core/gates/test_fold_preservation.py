"""``fold_body`` / ``check_fold_preserved`` — the never-close-for-real guarantee (#4344).

The backlog sweep groups aggressively and closes nothing for real: a member row is
retired only once its substance lives in an existing host. These pin the two halves of
that guarantee — the fold carries the body verbatim, and the check refuses a host body
that summarised instead of moving it (the discard a "real closure" actually is).
"""

from teatree.core.gates.fold_preservation import check_fold_preserved, fold_body, fold_marker

_MEMBER_BODY = """## The problem

`apply_lane_ceiling` drops force-keep items that were re-added after the cut.

## Acceptance

- A re-added force-keep item survives the ceiling.
"""

_HOST_BODY = """## The lane ceiling is applied twice

The ceiling runs before and after the force-keep pass.
"""


class TestFoldBody:
    def test_every_member_line_lands_in_the_host_body(self) -> None:
        folded = fold_body(
            host_body=_HOST_BODY,
            member_ref="#4247",
            member_title="force-keep items are dropped",
            member_body=_MEMBER_BODY,
        )
        for line in (line.strip() for line in _MEMBER_BODY.splitlines() if line.strip()):
            assert line in folded

    def test_the_host_body_survives_the_fold(self) -> None:
        folded = fold_body(host_body=_HOST_BODY, member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        assert "The ceiling runs before and after the force-keep pass." in folded

    def test_the_fold_is_recorded_under_the_member_ref(self) -> None:
        folded = fold_body(host_body=_HOST_BODY, member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        assert fold_marker("#4247") in folded

    def test_refolding_the_same_member_does_not_duplicate_it(self) -> None:
        once = fold_body(host_body=_HOST_BODY, member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        twice = fold_body(host_body=once, member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        assert twice == once

    def test_a_second_member_folds_alongside_the_first(self) -> None:
        once = fold_body(host_body=_HOST_BODY, member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        twice = fold_body(host_body=once, member_ref="#4150", member_title="u", member_body="A second idea.")
        assert fold_marker("#4247") in twice
        assert fold_marker("#4150") in twice
        assert "A second idea." in twice

    def test_an_empty_host_body_folds_without_leading_blanks(self) -> None:
        folded = fold_body(host_body="", member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        assert folded.startswith(fold_marker("#4247"))


class TestCheckFoldPreserved:
    def test_a_body_produced_by_fold_body_is_preserved(self) -> None:
        folded = fold_body(host_body=_HOST_BODY, member_ref="#4247", member_title="t", member_body=_MEMBER_BODY)
        assert check_fold_preserved(member_body=_MEMBER_BODY, host_body=folded) == ""

    def test_a_summarised_fold_is_refused(self) -> None:
        lossy = f"{_HOST_BODY}\n\n{fold_marker('#4247')} — force-keep items are dropped\n\nSee #4247.\n"
        refusal = check_fold_preserved(member_body=_MEMBER_BODY, host_body=lossy)
        assert refusal != ""
        assert "discarded" in refusal

    def test_the_refusal_names_what_was_dropped(self) -> None:
        lossy = f"{_HOST_BODY}\n\n## Acceptance\n"
        refusal = check_fold_preserved(member_body=_MEMBER_BODY, host_body=lossy)
        assert "## The problem" in refusal

    def test_reindentation_is_not_a_loss(self) -> None:
        host = "\n".join(f"    {line}" for line in _MEMBER_BODY.splitlines())
        assert check_fold_preserved(member_body=_MEMBER_BODY, host_body=host) == ""

    def test_an_empty_member_body_has_nothing_to_lose(self) -> None:
        assert check_fold_preserved(member_body="   \n\n", host_body=_HOST_BODY) == ""
