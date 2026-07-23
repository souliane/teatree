"""One autonomy decision governs BOTH the intake gate and the merge gate (#3577).

The trust RESOLVER (``classify_author`` / ``classify_pr_provenance``) was already
shared, but the POLICY built on it was restated at each gate — intake conjoined
explicit trusted-set membership by hand, the merge rungs read ``.untrusted`` by
hand. :func:`~teatree.core.review.author_trust.decide_author_trust` is the one
application both now call, so the two cannot disagree about who is trusted.

Handles here are SYNTHETIC.
"""

import pytest

from teatree.core.review import author_trust
from teatree.core.review.author_trust import AuthorSubject, AutonomyGate, TrustVerdict, decide_author_trust

_TRUSTED = frozenset({"owner-handle"})
_PUBLIC_SLUG = "someowner/public-repo"


@pytest.fixture(autouse=True)
def _public_repo_no_db_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PUBLIC repo with an empty DB trust set, so *extra_trusted* is the only trust source."""
    monkeypatch.setattr(author_trust, "repo_is_internal", lambda _slug, **_kw: False)
    monkeypatch.setattr(author_trust, "trusted_handles", set)


class TestBothGatesShareOneAnswer:
    @pytest.mark.parametrize("gate", list(AutonomyGate))
    def test_trusted_author_is_autonomous_at_every_gate(self, gate: AutonomyGate) -> None:
        subject = AuthorSubject(_PUBLIC_SLUG, "owner-handle")
        assert decide_author_trust(subject, gate=gate, extra_trusted=_TRUSTED) is TrustVerdict.AUTONOMOUS

    @pytest.mark.parametrize("gate", list(AutonomyGate))
    def test_stranger_is_held_for_human_review_at_every_gate(self, gate: AutonomyGate) -> None:
        subject = AuthorSubject(_PUBLIC_SLUG, "a-stranger")
        assert decide_author_trust(subject, gate=gate, extra_trusted=_TRUSTED) is TrustVerdict.HUMAN_REVIEW

    @pytest.mark.parametrize("gate", list(AutonomyGate))
    def test_empty_author_fails_closed_at_every_gate(self, gate: AutonomyGate) -> None:
        subject = AuthorSubject(_PUBLIC_SLUG, "")
        assert decide_author_trust(subject, gate=gate, extra_trusted=_TRUSTED) is TrustVerdict.HUMAN_REVIEW


class TestForkHoldsOnlyAtTheMergeGate:
    """A fork head branch always holds for a human — the merge gate's extra conjunct."""

    def test_trusted_author_fork_pr_is_held(self) -> None:
        subject = AuthorSubject(_PUBLIC_SLUG, "owner-handle", same_repo=False)
        verdict = decide_author_trust(subject, gate=AutonomyGate.MERGE, extra_trusted=_TRUSTED)
        assert verdict is TrustVerdict.HUMAN_REVIEW

    def test_unreported_provenance_falls_back_to_the_author_check(self) -> None:
        subject = AuthorSubject(_PUBLIC_SLUG, "owner-handle", same_repo=None)
        verdict = decide_author_trust(subject, gate=AutonomyGate.MERGE, extra_trusted=_TRUSTED)
        assert verdict is TrustVerdict.AUTONOMOUS


class TestIntakeIsStricterOnInternalRepos:
    """Intake requires EXPLICIT trust-set membership; the merge gate keeps the repo bypass."""

    def test_unlisted_author_on_an_internal_repo_splits_the_two_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(author_trust, "repo_is_internal", lambda _slug, **_kw: True)
        intake = decide_author_trust(
            AuthorSubject("owner/private", "unlisted"), gate=AutonomyGate.INTAKE, extra_trusted=_TRUSTED
        )
        merge = decide_author_trust(
            AuthorSubject("owner/private", "unlisted", same_repo=True), gate=AutonomyGate.MERGE, extra_trusted=_TRUSTED
        )
        assert intake is TrustVerdict.HUMAN_REVIEW
        assert merge is TrustVerdict.AUTONOMOUS
