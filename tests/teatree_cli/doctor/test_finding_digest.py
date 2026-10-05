"""The watchdog's finding IDENTITY — what makes two observations "the same finding".

The re-surface bug these pin: the owner DM was keyed on a hash of the RENDERED body,
and several doctor FAIL lines carry a volatile counter ("17 commit(s) behind" →
"18 commit(s) behind"). Every counter tick minted a fresh key, so an unchanged
condition re-DM'd on every watchdog pass — 192 copies of one finding set.
"""

from teatree.cli.doctor.finding_digest import finding_identity

_BEHIND_17 = "teatree clone at /opt/clone/teatree is 17 commit(s) behind origin/main — run `t3 update`"
_BEHIND_18 = "teatree clone at /opt/clone/teatree is 18 commit(s) behind origin/main — run `t3 update`"
_SKILL = "/opt/agent-skills/ac-reviewing-codebase/SKILL.md: requires unknown skill 'review'"
_WORKTREE_1 = "FAIL  Registered worktree 328 at /work/4463-foo/teatree never was a git checkout (no .git entry)"
_WORKTREE_2 = "FAIL  Registered worktree 329 at /work/4464-bar/teatree never was a git checkout (no .git entry)"


class TestFindingIdentity:
    def test_a_volatile_counter_does_not_change_the_identity(self) -> None:
        assert finding_identity(_BEHIND_17) == finding_identity(_BEHIND_18)

    def test_two_distinct_findings_keep_distinct_identities(self) -> None:
        assert finding_identity(_BEHIND_17) != finding_identity(_SKILL)

    def test_whitespace_reflow_does_not_change_the_identity(self) -> None:
        assert finding_identity("the  worker\tis   down") == finding_identity("the worker is down")
