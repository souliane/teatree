"""The fixer brief carries what a fixing agent needs to overrule it, and passes the dispatch anchor gate."""

from teatree.agents.fixer_brief import FixerBrief
from teatree.hooks import brief_anchor_scanner

_PR = "https://github.com/souliane/teatree/pull/5870"
_HEAD = "50ce18cd4b1f3a9e8d7c6b5a4f3e2d1c0b9a8f7e"
_BASE = "6a6f05c3c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6"
_FINDINGS = ("src/teatree/core/merge/execution.py:412 drops the base check", "there are 3 callers")


def _brief(*, base_sha: str = _BASE) -> FixerBrief:
    return FixerBrief(
        pr_url=_PR,
        head_sha=_HEAD,
        base_sha=base_sha,
        agent_type="debugging",
        escape="gate-escape:brief_anchor recorded by the owner",
        checkout_kind="ticket worktree on the PR head branch",
        findings=_FINDINGS,
    )


class TestFixerBriefRender:
    def test_the_brief_carries_the_trust_the_code_clause(self) -> None:
        assert brief_anchor_scanner.TRUST_THE_CODE_CLAUSE in _brief().render()

    def test_the_brief_anchors_head_and_base_with_verified_at(self) -> None:
        text = _brief().render()
        assert f"verified at {_HEAD}" in text
        assert f"verified at {_BASE}" in text

    def test_the_brief_names_the_agent_type_escape_and_checkout_kind(self) -> None:
        text = _brief().render()
        assert "debugging" in text
        assert "gate-escape:brief_anchor recorded by the owner" in text
        assert "ticket worktree on the PR head branch" in text

    def test_the_brief_lists_the_findings_under_the_pr(self) -> None:
        text = _brief().render()
        assert _PR in text
        assert "execution.py:412 drops the base check" in text

    def test_a_brief_full_of_specifics_passes_the_real_anchor_scanner(self) -> None:
        assert brief_anchor_scanner.find_unanchored_assertions(_brief().render()) is None

    def test_a_blank_base_drops_only_the_base_anchor(self) -> None:
        text = _brief(base_sha="").render()
        assert f"verified at {_HEAD}" in text
        assert "verified at  " not in text
        assert brief_anchor_scanner.find_unanchored_assertions(text) is None
