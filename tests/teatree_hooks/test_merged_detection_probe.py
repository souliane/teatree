"""Tests for the landed-ness probe detector — WHICH git shapes are the question (#2663).

Two primitives answer "has this branch landed?" wrongly on a squash-merging repo, and the
dream ledger names both: ``git merge-base --is-ancestor`` and ``git log <default>..<sha>``.
Neither is recognisable from the subcommand alone, so the discriminators are pinned here.

DIRECTION separates the two ``is-ancestor`` questions. ``is-ancestor <x> <default>`` asks "is
my work on main?" — the landed-ness question a squash defeats. ``is-ancestor <default> <x>``
asks the opposite, "has main reached my branch?" — the branch-currency check
``skills/review/SKILL.md`` prescribes before reviewing a diff, which a squash does not defeat.
Same subcommand, same flag, opposite meanings, so the operand ORDER is the whole tell.

The ``git log`` range arm is bounded to a SHA on the right because ``origin/main..HEAD`` and
``origin/main..<branch>`` are the routine reads ``skills/debug`` and ``skills/workspace``
prescribe; only a pinned SHA on the right reads as interrogating one landed commit.
"""

import pytest

from teatree.hooks.merged_detection_probe import merged_detection_shape

_RED, _GREEN = "a" * 40, "b" * 40


class TestIsAncestorDirection:
    @pytest.mark.parametrize(
        "command",
        [
            "git merge-base --is-ancestor HEAD origin/main",
            "git merge-base --is-ancestor HEAD main",
            "git merge-base --is-ancestor HEAD origin/master",
            "git merge-base --is-ancestor HEAD master",
            f"git merge-base --is-ancestor {_RED} origin/main",
        ],
    )
    def test_default_branch_last_is_the_landed_ness_question(self, command: str) -> None:
        assert merged_detection_shape(command) == "git merge-base --is-ancestor against the default branch"

    @pytest.mark.parametrize(
        "command",
        [
            "git merge-base --is-ancestor origin/main HEAD",
            "git merge-base --is-ancestor origin/main HEAD || git merge origin/main --no-edit",
            "git merge-base --is-ancestor main HEAD",
            "git merge-base --is-ancestor origin/master HEAD",
            f"git merge-base --is-ancestor origin/main {_GREEN}",
        ],
    )
    def test_default_branch_first_is_the_currency_check(self, command: str) -> None:
        assert merged_detection_shape(command) is None

    def test_two_sha_provenance_proof_names_no_default_branch(self) -> None:
        assert merged_detection_shape(f"git merge-base --is-ancestor {_RED} {_GREEN}") is None

    def test_operandless_invocation_does_not_raise(self) -> None:
        assert merged_detection_shape("git merge-base --is-ancestor") is None


class TestLogRangeAgainstTheDefaultBranch:
    @pytest.mark.parametrize(
        "command",
        [
            "git log origin/main..84df33a",
            f"git log origin/main..{_RED}",
            "git log main..84df33adff",
            "git log origin/master..84df33adff",
            "git log --oneline origin/main..84df33adff -- src/x.py",
            "git -C /some/worktree log origin/main..84df33adff",
            "cd /some/worktree && git log origin/main..84df33adff",
        ],
    )
    def test_a_pinned_sha_on_the_right_is_the_landed_ness_question(self, command: str) -> None:
        assert merged_detection_shape(command) == "git log <default branch>..<sha>"

    @pytest.mark.parametrize(
        "command",
        [
            "git log origin/main..HEAD --oneline",
            "git log --oneline origin/main..HEAD -- src/x.py",
            "git log origin/main..feature-branch",
            "git log origin/main..HEAD~3",
            "git log origin/main..$SHA",
            "git log origin/main..abc",
            "git log origin/main...84df33adff",
            "git diff origin/main...HEAD",
            "git log upstream/topic..84df33adff",
        ],
    )
    def test_the_routine_range_reads_stay_silent(self, command: str) -> None:
        assert merged_detection_shape(command) is None

    def test_the_not_form_keeps_its_own_label(self) -> None:
        assert merged_detection_shape("git log HEAD --not origin/main") == "git log --not <default branch>"


class TestTheProbeMustBeAnInvocation:
    @pytest.mark.parametrize(
        "command",
        [
            "echo 'git log origin/main..84df33adff'",
            "grep -rn 'git merge-base --is-ancestor HEAD origin/main' docs/",
        ],
    )
    def test_quoted_text_is_not_an_invocation(self, command: str) -> None:
        assert merged_detection_shape(command) is None

    def test_a_wrapper_leader_does_not_hide_the_probe(self) -> None:
        assert merged_detection_shape("env FOO=1 git log origin/main..84df33adff") is not None
