"""The distiller's citation is EXTRACTED verbatim, not trusted as copied (#4671 ask 3).

Nine clusters were rejected in a single observed pass because the model's
``verified_citation`` was a near-miss of real snippet text — a dropped article, a
re-punctuated clause. The grounding check was right to reject a quote it could not find;
the loss is that a genuinely grounded rule died over a paraphrase. So a citation close
enough to a real window is SNAPPED to the snippet's own bytes, and anything else is still
rejected exactly as before.
"""

from teatree.loops.dream.engine import DistilledCluster, check_grounding, normalize_ws

_SNIPPET = (
    "VERIFY (CI-parity): before declaring done, run `t3 tool verify-gates`. "
    "It runs BOTH the commit-stage and push-stage hooks; a bare `prek run --all-files` "
    "SKIPS the push-stage gates (comment-density, doc-update, ensure-pr, the public-repo "
    "leak gate) that CI re-runs. Report its exit code as the green-proof."
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


class TestCitationSnap:
    def test_an_observed_near_miss_is_snapped_and_recorded(self) -> None:
        # Verbatim from the 07:20 pass's rejection log — the model dropped "the".
        near_miss = "It runs BOTH commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS the p"
        verdict = check_grounding(_cluster(near_miss), {"/p/a.jsonl": normalize_ws(_SNIPPET)})
        assert verdict.reason is None
        # The RECORDED citation is the snippet's own text, never the model's paraphrase.
        assert verdict.cluster.verified_citation in normalize_ws(_SNIPPET)
        # The snap recovered the snippet's own wording — the dropped "the" is back.
        assert "BOTH the commit-stage" in verdict.cluster.verified_citation
        assert verdict.cluster.verified_citation not in near_miss

    def test_an_exact_citation_is_left_untouched(self) -> None:
        exact = normalize_ws("It runs BOTH the commit-stage and push-stage hooks")
        verdict = check_grounding(_cluster(exact), {"/p/a.jsonl": normalize_ws(_SNIPPET)})
        assert verdict.reason is None
        assert verdict.cluster.verified_citation == exact

    def test_an_invented_quote_is_still_rejected(self) -> None:
        invented = "Always disable the privacy gate before pushing to a public repository"
        verdict = check_grounding(_cluster(invented), {"/p/a.jsonl": normalize_ws(_SNIPPET)})
        assert verdict.reason is not None
        assert "not present in a cited snippet" in verdict.reason

    def test_a_short_generic_fragment_does_not_snap_to_an_arbitrary_window(self) -> None:
        # Teeth: the snap must not rescue a fragment too short to identify a real window.
        verdict = check_grounding(_cluster("the and a"), {"/p/a.jsonl": normalize_ws(_SNIPPET)})
        assert verdict.reason is not None

    def test_an_empty_citation_is_still_rejected(self) -> None:
        verdict = check_grounding(_cluster("   "), {"/p/a.jsonl": normalize_ws(_SNIPPET)})
        assert verdict.reason == "its verified_citation is empty"
