"""The distiller's citation is EXTRACTED verbatim, not trusted as copied (#4671 ask 3).

Nine clusters were rejected in a single observed pass because the model's
``verified_citation`` was a near-miss of real snippet text — a dropped article, a
re-punctuated clause. The grounding check was right to reject a quote it could not find;
the loss is that a genuinely grounded rule died over a paraphrase. So a citation close
enough to a real window is SNAPPED to the snippet's own bytes, and anything else is still
rejected exactly as before.

``SequenceMatcher.ratio()`` is character-level and therefore polarity-blind — a
single-token inversion scores 0.9818 against the observed dropped-article paraphrase's
0.9149, so every threshold admitting the paraphrase admits the inversion (#4716). The ratio
LOCATES the window; admission is decided by the token delta, and these tests pin both sides
of that bound.
"""

from difflib import SequenceMatcher

import pytest

from teatree.loops.dream.citation_snap import (
    _SNAP_ADMISSIBLE_DELTA,
    _SNAP_MIN_CITATION_CHARS,
    _SNAP_NEGATING_SUFFIXES,
    _SNAP_NEGATORS,
    _anchored_windows,
    _window_bounds,
)
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
#: A negating PREFIX far enough into the sentence that a citation can end on the bare stem.
#: `_AFFIX_SNIPPET` cannot host that shape — its `disallowed` sits so near the start that a
#: citation ending there is under the length floor and is refused before the head is read.
_HEAD_AFFIX_TAIL_SNIPPET = (
    "The keystone gate holds a merge whose branch the reviewer authored itself, because "
    "clearing a review on your own work is disallowed. A second reviewer must record the "
    "clear before the merge proceeds."
)
#: Verbatim from the 07:20 pass's rejection log — the model dropped "the".
_OBSERVED_NEAR_MISS = "It runs BOTH commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS the p"
_INVERTED_CITATION = (
    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
    "RUNS the push-stage gates (comment-density, doc-update, ensure-pr, the public-repo "
    "leak gate) that CI re-runs."
)
_INSERTED_NEVER_CITATION = (
    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
    "never SKIPS the push-stage gates (comment-density, doc-update, ensure-pr, the "
    "public-repo leak gate) that CI re-runs."
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


#: A contracted negation, whose stem is a real word asserting the opposite of the whole.
_CONTRACTED_SNIPPET = (
    "The affected lane selects the tests the diff can reach, so it doesn't run the whole "
    "suite on every push. A lane that cannot classify a path escalates instead."
)
#: Irregular contractions whose stem is NOT stem+`n't` — `can't` is `ca`+`n't`, `won't` is
#: `wo`+`n't` — so the apostrophe itself, not an `n`, is the only boundary signal left.
_IRREGULAR_CONTRACTION_SNIPPET = (
    "The keystone refuses the merge because a stale branch can't clear the gate twice on the same head."
)
_WONT_CONTRACTION_SNIPPET = (
    "Under the autonomous-merge settings a reviewer won't approve a change that touches their own worktree."
)
#: A possessive puts the same stem-plus-apostrophe shape on an ordinary noun, not a negator.
_POSSESSIVE_SNIPPET = (
    "Two agents in one working tree interleave commits, so a dispatched agent's checkout "
    "is claimed for the length of its run."
)


#: A negator governing the clause a quote can start just after, dropping it.
_ADJACENT_NEGATOR_SNIPPET = (
    "The push path is guarded twice. Before a public push the agent must never disable the "
    "privacy gate before pushing to a public repository, because the leak gate re-runs in CI "
    "and a bypass there is permanent."
)
#: The same shape at the other edge: a quote can stop just before the negator.
_TRAILING_NEGATOR_SNIPPET = (
    "The keystone refuses the second attempt on the same head because a repeat write there "
    "is never idempotent, and the audit row says so."
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
        near_miss = _OBSERVED_NEAR_MISS
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
        near_miss = _OBSERVED_NEAR_MISS
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
        verdict = _verdict(_INVERTED_CITATION)
        assert verdict.reason is not None
        assert "runs" in verdict.reason
        assert "skips" in verdict.reason
        # Not the wholly-invented reason: this quote DID locate a real window.
        assert "not present in a cited snippet" not in verdict.reason

    @pytest.mark.parametrize(
        ("label", "citation", "snippet"),
        [
            ("inserted-never", _INSERTED_NEVER_CITATION, _SNIPPET),
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

    def test_a_head_negation_at_the_citation_tail_is_refused(self) -> None:
        """The only shape a head carve-out can excuse, so the only one that pins it closed.

        Two of the three cases above put their affix at the citation's START, where the
        positional guard short-circuits first; `harmless` -> `harm` reaches the check on the
        `startswith` side. So reinstating `word.endswith(cut)` left every one of them green
        while a head negation landing at the TAIL — `disallowed` quoted as `allowed` — was
        admitted and re-extracted to the snippet's own `disallowed`, recording a quote
        asserting the opposite of its own rule (#4716).
        """
        head_negated = (
            "The keystone gate holds a merge whose branch the reviewer authored itself, because "
            "clearing a review on your own work is allowed"
        )
        verdict = _verdict(head_negated, _HEAD_AFFIX_TAIL_SNIPPET)
        assert verdict.reason is not None
        # Anti-vacuity: it LOCATED a real window, so the refusal is the delta rule's own and
        # not the cheaper "could not find it" this test would otherwise pass on.
        assert "not present in a cited snippet" not in verdict.reason
        assert "'allowed' where the snippet has 'disallowed" in verdict.reason

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


class TestTheRatioCannotSeparateThem:
    """The premise the whole token-delta rule rests on, measured rather than asserted (#4716).

    Both module docstrings quote these digits as the reason a threshold cannot do the job.
    Nothing reproduced them, and one had already drifted to a figure that argued the opposite
    way round — so the claim is measured here and the digits cannot go stale again.
    """

    @staticmethod
    def _best_ratio(citation: str, snippet: str) -> float:
        """The highest ratio `snap_citation` would score for *citation*, over the same windows."""
        text = normalize_ws(snippet)
        best = 0.0
        for start in _anchored_windows(citation, text):
            left, right = _window_bounds(text, start, len(citation))
            best = max(best, SequenceMatcher(None, text[left:right], citation).ratio())
        return best

    def test_both_inversions_outscore_the_paraphrase_the_snap_exists_to_rescue(self) -> None:
        inverted = self._best_ratio(_INVERTED_CITATION, _SNIPPET)
        negated = self._best_ratio(_INSERTED_NEVER_CITATION, _SNIPPET)
        paraphrase = self._best_ratio(_OBSERVED_NEAR_MISS, _SNIPPET)
        assert (round(inverted, 4), round(negated, 4), round(paraphrase, 4)) == (0.9818, 0.9674, 0.9149)
        # The consequence: every threshold low enough to admit the paraphrase admits both
        # meaning-inverting quotes, which is why admission is a token-delta decision.
        assert paraphrase < negated < inverted


class TestAMidWordEdgeIsNotVerbatim:
    """A substring is not a quote: `does` sits inside `doesn't`, `allowed` inside `disallowed`."""

    def test_a_citation_cut_out_of_a_contracted_negation_is_refused(self) -> None:
        cut_from_contraction = "The affected lane selects the tests the diff can reach, so it does"
        assert cut_from_contraction in normalize_ws(_CONTRACTED_SNIPPET)
        assert _verdict(cut_from_contraction, _CONTRACTED_SNIPPET).reason is not None

    def test_a_citation_cut_out_of_a_negating_prefix_is_refused(self) -> None:
        cut_from_prefix = "allowed from clearing the merge on a branch it authored itself"
        assert cut_from_prefix in normalize_ws(_AFFIX_SNIPPET)
        verdict = _verdict(cut_from_prefix, _AFFIX_SNIPPET)
        assert verdict.reason is not None
        assert "disallowed" in verdict.reason

    def test_a_word_bounded_quote_is_still_admitted_untouched(self) -> None:
        # The control: word-bounding the test must not cost an ordinary verbatim citation.
        exact = normalize_ws("Retrying the publish step after a timeout is harmless")
        verdict = _verdict(exact, _AFFIX_SNIPPET)
        assert verdict.reason is None
        assert verdict.cluster.verified_citation == exact

    def test_a_mid_word_tail_cut_still_falls_through_to_the_snap(self) -> None:
        # The control the word-bounded test must not break: a cut into an ordinary word is
        # no longer verbatim, so the snap decides it and re-extracts the whole word.
        cut_into_idempotent = (
            "The keystone refuses the second attempt on the same head because a repeat write there is never idemp"
        )
        verdict = _verdict(cut_into_idempotent, _TRAILING_NEGATOR_SNIPPET)
        assert verdict.reason is None
        assert "never idempotent" in verdict.cluster.verified_citation


class TestAQuoteCutAtANegatorIsRefused:
    """Re-extracting a span cut away from its own negator records the opposite rule (#2663)."""

    @pytest.mark.parametrize(
        ("label", "citation", "snippet"),
        [
            (
                "verbatim-after-never",
                ("disable the privacy gate before pushing to a public repository, because the leak gate re-runs in CI"),
                _ADJACENT_NEGATOR_SNIPPET,
            ),
            (
                "snapped-after-never",
                ("disable privacy gate before pushing to a public repository, because the leak gate re-runs in CI"),
                _ADJACENT_NEGATOR_SNIPPET,
            ),
            (
                "verbatim-before-never",
                "The keystone refuses the second attempt on the same head because a repeat write there is",
                _TRAILING_NEGATOR_SNIPPET,
            ),
        ],
    )
    def test_a_quote_cut_at_a_negator_is_refused(self, label: str, citation: str, snippet: str) -> None:
        verdict = _verdict(citation, snippet)
        assert verdict.reason is not None, label
        # Anti-vacuity: it DID quote a real window, so the refusal is this rule's own and not
        # the cheaper "could not find it" the test would otherwise pass on.
        assert "not present in a cited snippet" not in verdict.reason, label
        assert "never" in verdict.reason, label

    def test_a_quote_past_a_clause_break_is_admitted(self) -> None:
        # The control: only the ADJACENT token is read, so a negator in the previous sentence
        # leaves the quote alone.
        past_the_break = normalize_ws(
            "Before a public push the agent must never disable the privacy gate before pushing"
        )
        assert _verdict(past_the_break, _ADJACENT_NEGATOR_SNIPPET).reason is None

    def test_a_quote_that_keeps_the_negator_is_admitted(self) -> None:
        # The control that keeps the rule honest: a dropped article beside a RETAINED negator
        # is the paraphrase the snap exists to rescue.
        keeps_never = "keystone refuses the second attempt on the same head because a repeat write there is never"
        verdict = _verdict(keeps_never, _TRAILING_NEGATOR_SNIPPET)
        assert verdict.reason is None
        assert "is never" in verdict.cluster.verified_citation


class TestAContractedNegationIsNotATruncatedEdge:
    """`doesn't` -> `does` wears a tail cut's shape while asserting the opposite (#2663)."""

    def test_a_tail_cut_at_the_contraction_boundary_is_refused(self) -> None:
        # Dropping an article keeps the strict test away, so the snap's carve-out decides.
        cut_at_the_boundary = "The affected lane selects tests the diff can reach, so it does"
        assert _verdict(cut_at_the_boundary, _CONTRACTED_SNIPPET).reason is not None

    def test_a_tail_cut_past_the_contraction_boundary_is_still_admitted(self) -> None:
        # The control: `doe` is no word, so re-extraction inverts nothing and the rescue holds.
        cut_past_the_boundary = "The affected lane selects tests the diff can reach, so it doe"
        assert _verdict(cut_past_the_boundary, _CONTRACTED_SNIPPET).reason is None

    def test_the_negating_suffixes_are_the_ones_that_leave_a_word_behind(self) -> None:
        assert _SNAP_NEGATING_SUFFIXES == ("less", "n't", "'t")


class TestAnIrregularContractionIsNotATruncatedEdge:
    """`can't` is `ca`+`n't` and `won't` is `wo`+`n't` — the stem is not stem+`n't` (#2663).

    The `does` <- `doesn't` fix reads the removed tail for the `n't` suffix; that tail is
    `'t` here, not `n't`, so the same word-boundary hole it closed stays open for every
    irregular contraction and for an ordinary possessive shaped the same way.
    """

    def test_a_citation_cut_out_of_cant_is_refused(self) -> None:
        cut_from_cant = "The keystone refuses the merge because a stale branch can"
        assert cut_from_cant in normalize_ws(_IRREGULAR_CONTRACTION_SNIPPET)
        verdict = _verdict(cut_from_cant, _IRREGULAR_CONTRACTION_SNIPPET)
        assert verdict.reason is not None

    def test_a_citation_cut_out_of_wont_is_refused(self) -> None:
        cut_from_wont = "Under the autonomous-merge settings a reviewer won"
        assert cut_from_wont in normalize_ws(_WONT_CONTRACTION_SNIPPET)
        verdict = _verdict(cut_from_wont, _WONT_CONTRACTION_SNIPPET)
        assert verdict.reason is not None

    def test_a_citation_cut_before_a_possessive_s_is_still_rescued(self) -> None:
        # The control: `'s` is not `_SNAP_NEGATING_SUFFIXES` — a possessive drop changes no
        # polarity, so it stays the same legitimate rescue a dropped article gets, and the
        # snap re-extracts the full possessive rather than refusing the near-miss.
        cut_before_possessive = normalize_ws("Two agents in one working tree interleave commits, so a dispatched agent")
        verdict = _verdict(cut_before_possessive, _POSSESSIVE_SNIPPET)
        assert verdict.reason is None
        assert verdict.cluster.verified_citation.endswith("agent's")

    def test_a_citation_cut_out_of_cant_via_the_snap_path_is_refused(self) -> None:
        # Isolates the SUFFIX fix from the boundary fix: an article drop forces this off the
        # strict path regardless of boundary handling, so only the snap's carve-out decides.
        # (Not mirrored for `won't`: a different anchored window already catches that one
        # through `negated_edge`, so a same-shaped won't test would be vacuous here.)
        cut_from_cant_and_an_article = "The keystone refuses merge because a stale branch can"
        verdict = _verdict(cut_from_cant_and_an_article, _IRREGULAR_CONTRACTION_SNIPPET)
        assert verdict.reason is not None
        assert "not present in a cited snippet" not in verdict.reason

    def test_a_citation_cut_right_after_the_possessive_apostrophe_is_refused(self) -> None:
        # The mirror hole on the LEFT edge: a citation starting just past the apostrophe.
        cut_after_apostrophe = "s checkout is claimed for the length of its run."
        assert cut_after_apostrophe in normalize_ws(_POSSESSIVE_SNIPPET)
        verdict = _verdict(cut_after_apostrophe, _POSSESSIVE_SNIPPET)
        assert verdict.reason is not None

    def test_a_citation_through_the_whole_contraction_is_still_admitted(self) -> None:
        # The control: quoting PAST the apostrophe, not stopping at it, is a real quote.
        whole_word = normalize_ws("The keystone refuses the merge because a stale branch can't clear the gate")
        verdict = _verdict(whole_word, _IRREGULAR_CONTRACTION_SNIPPET)
        assert verdict.reason is None
        assert verdict.cluster.verified_citation == whole_word

    def test_a_citation_through_the_whole_possessive_is_still_admitted(self) -> None:
        # The control: `agent's` quoted whole is not a truncation of anything.
        whole_word = normalize_ws("so a dispatched agent's checkout is claimed for the length of its run")
        verdict = _verdict(whole_word, _POSSESSIVE_SNIPPET)
        assert verdict.reason is None
        assert verdict.cluster.verified_citation == whole_word
