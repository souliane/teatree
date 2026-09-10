"""The distiller's citation is EXTRACTED verbatim, not trusted as copied (#4671 ask 3).

Nine clusters were rejected in a single observed pass because the model's
``verified_citation`` was a near-miss of real snippet text — a dropped article, a
re-punctuated clause. The grounding check was right to reject a quote it could not find;
the loss is that a genuinely grounded rule died over a paraphrase. So a citation close
enough to a real window is SNAPPED to the snippet's own bytes, and anything else is still
rejected exactly as before.

``SequenceMatcher.ratio()`` is character-level and therefore polarity-blind — a
single-token inversion scores 0.9818 against a dropped-article paraphrase's 0.9895, so no
threshold can separate them (#4716). The ratio LOCATES the window; admission is decided by
the token delta, and these tests pin both sides of that bound.
"""

import pytest

from teatree.loops.dream.citation_snap import _SNAP_ADMISSIBLE_DELTA, _SNAP_MIN_CITATION_CHARS, _SNAP_NEGATORS
from teatree.loops.dream.engine import DistilledCluster, GroundingVerdict, check_grounding, normalize_ws

_SNIPPET = (
    "VERIFY (CI-parity): before declaring done, run `t3 tool verify-gates`. "
    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
    "SKIPS the push-stage gates (comment-density, doc-update, ensure-pr, the public-repo "
    "leak gate) that CI re-runs. Report its exit code as the green-proof."
)
_NEGATED_SNIPPET = (
    "The affected lane does not run the whole suite; it selects the tests the diff can "
    "reach and escalates to the full sweep on a migration or a conftest edit. A lane that "
    "cannot classify a path runs everything rather than under-running the change."
)
_CONNECTIVE_SNIPPET = (
    "The shipper must run the lint gate and the test gate before it opens the pull request "
    "for review. A red pipeline or a stale branch holds the merge, so the shipper waits on "
    "the tip and reports by comment."
)
_AFFIX_SNIPPET = (
    "A reviewer is disallowed from clearing the merge on a branch it authored itself. "
    "Retrying the publish step after a timeout is harmless when the write it repeats is "
    "idempotent. An unsafe rerun of a publish step is refused by the keystone gate before "
    "it records anything."
)
_NEGATOR_TAIL_SNIPPET = (
    "The publish step must never run twice on the same head, and the keystone refuses the "
    "second attempt because a repeat write is never idempotent."
)
#: Tokens the allowlist may never carry. Swapping any of them for another admissible token
#: changes what the sentence asserts, so a snap over one re-extracts a different rule.
_ASSERTION_BEARING = frozenset(
    {
        "and",
        "or",
        "nor",
        "but",
        "so",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "as",
        "with",
        "by",
        "from",
        "into",
        "its",
        "that",
        "this",
    }
)


def _cluster(citation: str) -> DistilledCluster:
    return DistilledCluster(
        cluster_key="k",
        rule="Run `t3 tool verify-gates` before declaring done.",
        source_files=["/p/a.jsonl"],
        is_binding=False,
        verified_citation=citation,
        durable_destination="d",
    )


def _verdict(citation: str, snippet: str = _SNIPPET) -> GroundingVerdict:
    return check_grounding(_cluster(citation), {"/p/a.jsonl": normalize_ws(snippet)})


class TestCitationSnap:
    def test_an_observed_near_miss_is_snapped_and_recorded(self) -> None:
        # Verbatim from the 07:20 pass's rejection log — the model dropped "the".
        near_miss = "It runs BOTH commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS the p"
        verdict = _verdict(near_miss)
        assert verdict.reason is None
        # The RECORDED citation is the snippet's own text, never the model's paraphrase.
        assert verdict.cluster.verified_citation in normalize_ws(_SNIPPET)
        # The snap recovered the snippet's own wording — the dropped "the" is back.
        assert "BOTH the commit-stage" in verdict.cluster.verified_citation
        assert verdict.cluster.verified_citation not in near_miss

    def test_an_exact_citation_is_left_untouched(self) -> None:
        exact = normalize_ws("It runs BOTH the commit-stage and push-stage hooks")
        verdict = _verdict(exact)
        assert verdict.reason is None
        assert verdict.cluster.verified_citation == exact

    def test_an_invented_quote_is_still_rejected(self) -> None:
        invented = "Always disable the privacy gate before pushing to a public repository"
        verdict = _verdict(invented)
        assert verdict.reason is not None
        assert "not present in a cited snippet" in verdict.reason

    def test_a_citation_below_the_length_floor_is_not_snapped(self) -> None:
        # Teeth: the SAME paraphrase snaps above the floor and is refused below it.
        near_miss = "It runs BOTH commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS the p"
        assert _verdict(near_miss).reason is None
        truncated = near_miss[: _SNAP_MIN_CITATION_CHARS - 1]
        assert len(truncated) < _SNAP_MIN_CITATION_CHARS
        assert _verdict(truncated).reason is not None

    def test_an_empty_citation_is_still_rejected(self) -> None:
        verdict = _verdict("   ")
        assert verdict.reason == "its verified_citation is empty"


class TestComposedQuoteIsRefused:
    """A located window is not enough — the token delta decides admission (#4716)."""

    def test_a_single_token_inversion_is_rejected(self) -> None:
        inverted = (
            "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
            "RUNS the push-stage gates (comment-density, doc-update, ensure-pr, the public-repo "
            "leak gate) that CI re-runs."
        )
        verdict = _verdict(inverted)
        assert verdict.reason is not None
        assert "runs" in verdict.reason
        assert "skips" in verdict.reason
        # Not the wholly-invented reason: this quote DID locate a real window.
        assert "not present in a cited snippet" not in verdict.reason

    @pytest.mark.parametrize(
        ("label", "citation", "snippet"),
        [
            (
                "inserted-never",
                (
                    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
                    "never SKIPS the push-stage gates (comment-density, doc-update, ensure-pr, the "
                    "public-repo leak gate) that CI re-runs."
                ),
                _SNIPPET,
            ),
            (
                "inserted-does-not",
                (
                    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
                    "does not SKIPS the push-stage gates (comment-density, doc-update, ensure-pr, the "
                    "public-repo leak gate) that CI re-runs."
                ),
                _SNIPPET,
            ),
            (
                "prepended-never",
                (
                    "Never SKIPS the push-stage gates (comment-density, doc-update, ensure-pr, the "
                    "public-repo leak gate) that CI re-runs."
                ),
                _SNIPPET,
            ),
            (
                "dropped-not",
                (
                    "The affected lane does run the whole suite; it selects the tests the diff can "
                    "reach and escalates to the full sweep on a migration or a conftest edit."
                ),
                _NEGATED_SNIPPET,
            ),
        ],
    )
    def test_a_negator_in_the_delta_is_rejected(self, label: str, citation: str, snippet: str) -> None:
        verdict = _verdict(citation, snippet)
        assert verdict.reason is not None, label
        assert "not present in a cited snippet" not in verdict.reason

    def test_a_content_word_substitution_is_rejected(self) -> None:
        substituted = (
            "It runs BOTH the commit-stage and push-stage tests; a bare `prek run --all-files` "
            "SKIPS the push-stage gates"
        )
        verdict = _verdict(substituted)
        assert verdict.reason is not None
        assert "tests" in verdict.reason
        assert "hooks" in verdict.reason

    def test_the_admissible_delta_carries_no_meaning(self) -> None:
        assert not _SNAP_ADMISSIBLE_DELTA & _SNAP_NEGATORS
        assert max(len(token) for token in _SNAP_ADMISSIBLE_DELTA) <= 4
        # Polarity and length are not enough — `and`/`or` clear both and still flip a rule.
        assert not _SNAP_ADMISSIBLE_DELTA & _ASSERTION_BEARING


class TestParaphraseIsStillAdmitted:
    """The control: a bound that also rejects these has tightened the snap into uselessness."""

    @pytest.mark.parametrize(
        ("label", "citation"),
        [
            (
                "dropped-article",
                (
                    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
                    "SKIPS push-stage gates"
                ),
            ),
            (
                "two-dropped-articles",
                "It runs BOTH commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS push-stage gates",
            ),
            (
                "swapped-article",
                (
                    "It runs BOTH the commit-stage and push-stage hooks; the bare `prek run --all-files` "
                    "SKIPS the push-stage gates"
                ),
            ),
            (
                "re-punctuated",
                (
                    "It runs BOTH the commit-stage and push-stage hooks. A bare 'prek run --all-files' "
                    "SKIPS the push-stage gates"
                ),
            ),
        ],
    )
    def test_a_paraphrase_is_snapped_to_the_snippets_own_bytes(self, label: str, citation: str) -> None:
        verdict = _verdict(citation)
        assert verdict.reason is None, label
        assert verdict.cluster.verified_citation in normalize_ws(_SNIPPET)


class TestASwappedConnectiveIsRefused:
    """A connective or preposition is not meaning-free — swapping one rewrites the rule (#4716)."""

    @pytest.mark.parametrize(
        ("label", "citation"),
        [
            (
                "and->or",
                "The shipper must run the lint gate or the test gate before it opens the pull request for review.",
            ),
            (
                "or->and",
                (
                    "A red pipeline and a stale branch holds the merge, so the shipper waits on the tip and "
                    "reports by comment."
                ),
            ),
            (
                "for->to",
                "The shipper must run the lint gate and the test gate before it opens the pull request to review.",
            ),
            (
                "on->at",
                (
                    "A red pipeline or a stale branch holds the merge, so the shipper waits at the tip and "
                    "reports by comment."
                ),
            ),
            (
                "by->with",
                (
                    "A red pipeline or a stale branch holds the merge, so the shipper waits on the tip and "
                    "reports with comment."
                ),
            ),
        ],
    )
    def test_a_swapped_connective_is_rejected(self, label: str, citation: str) -> None:
        verdict = _verdict(citation, _CONNECTIVE_SNIPPET)
        assert verdict.reason is not None, label
        # It DID locate a real window, so the refusal must name the swap, not call it invented.
        assert "not present in a cited snippet" not in verdict.reason, label


class TestAMorphologicalNegationIsRefused:
    """A negating affix wears the prefix/suffix shape the truncated-edge carve-out reads (#4716)."""

    @pytest.mark.parametrize(
        ("label", "citation"),
        [
            ("disallowed->allowed", "allowed from clearing the merge on branch it authored itself."),
            ("harmless->harm", "Retrying publish step after a timeout is harm"),
            (
                "unsafe->safe",
                "safe rerun of a publish step is refused by keystone gate before it records anything.",
            ),
        ],
    )
    def test_a_negating_affix_is_not_a_truncated_edge(self, label: str, citation: str) -> None:
        verdict = _verdict(citation, _AFFIX_SNIPPET)
        assert verdict.reason is not None, label

    def test_a_tail_cut_into_a_negator_is_rejected(self) -> None:
        cut_into_never = (
            "The publish step must never run twice on the same head, and keystone refuses the second "
            "attempt because a repeat write is nev"
        )
        assert _verdict(cut_into_never, _NEGATOR_TAIL_SNIPPET).reason is not None

    def test_a_tail_cut_into_an_ordinary_word_is_still_admitted(self) -> None:
        # The control: refusing this too would have closed the tail carve-out the snap needs.
        cut_into_idempotent = (
            "The publish step must never run twice on the same head, and keystone refuses the second "
            "attempt because a repeat write is never idemp"
        )
        assert _verdict(cut_into_idempotent, _NEGATOR_TAIL_SNIPPET).reason is None
