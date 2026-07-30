"""The watchdog's finding IDENTITY — what makes two observations "the same finding".

The re-surface bug these pin: the owner DM was keyed on a hash of the RENDERED body,
and several doctor FAIL lines carry a volatile counter ("17 commit(s) behind" →
"18 commit(s) behind"). Every counter tick minted a fresh key, so an unchanged
condition re-DM'd on every watchdog pass — 192 copies of one finding set.
"""

from teatree.cli.doctor.finding_digest import finding_identity, findings_digest

_BEHIND_17 = "teatree clone at /opt/clone/teatree is 17 commit(s) behind origin/main — run `t3 update`"
_BEHIND_18 = "teatree clone at /opt/clone/teatree is 18 commit(s) behind origin/main — run `t3 update`"
_SKILL = "/opt/agent-skills/ac-reviewing-codebase/SKILL.md: requires unknown skill 'review'"


class TestFindingIdentity:
    def test_a_volatile_counter_does_not_change_the_identity(self) -> None:
        assert finding_identity(_BEHIND_17) == finding_identity(_BEHIND_18)

    def test_two_distinct_findings_keep_distinct_identities(self) -> None:
        assert finding_identity(_BEHIND_17) != finding_identity(_SKILL)

    def test_whitespace_reflow_does_not_change_the_identity(self) -> None:
        assert finding_identity("the  worker\tis   down") == finding_identity("the worker is down")


class TestFindingsDigest:
    def test_unchanged_finding_set_digests_the_same_despite_counter_drift(self) -> None:
        assert findings_digest([_SKILL, _BEHIND_17]) == findings_digest([_SKILL, _BEHIND_18])

    def test_order_does_not_change_the_digest(self) -> None:
        assert findings_digest([_SKILL, _BEHIND_17]) == findings_digest([_BEHIND_17, _SKILL])

    def test_an_added_finding_changes_the_digest(self) -> None:
        assert findings_digest([_SKILL]) != findings_digest([_SKILL, _BEHIND_17])

    def test_a_removed_finding_changes_the_digest(self) -> None:
        assert findings_digest([_SKILL, _BEHIND_17]) != findings_digest([_BEHIND_17])

    def test_no_findings_digest_to_the_empty_marker(self) -> None:
        assert findings_digest([]) == ""
        assert findings_digest(["", "   "]) == ""

    def test_the_digest_is_short_and_hex(self) -> None:
        digest = findings_digest([_SKILL])
        assert len(digest) == 16
        assert all(char in "0123456789abcdef" for char in digest)
