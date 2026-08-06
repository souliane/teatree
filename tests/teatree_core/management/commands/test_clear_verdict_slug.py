"""The CLEAR's by-product verdict is keyed where the MERGE will look for it.

``resolve_pr_repo_slug`` is only half of the keystone's resolution; the other half is
the #1335 cross-repo reconcile. Issuance ran only the first half, so a CLEAR for a
downstream overlay's PR recorded its verdict under the running clone's ``origin`` while
the merge queried the recovered repo — a CLEAR unmergeable for its whole life, with
re-issuing reproducing the same wrong slug.
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.ticket import ClearIssueResult, _verdict_slug
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models import ClearRequest, MergeClear, ReviewVerdict

_SHA = "b" * 40
_RECONCILE = "teatree.core.management.commands.ticket._reconcile_slug_against_reviewed_sha"


def _request(slug: str) -> ClearRequest:
    return ClearRequest(
        pr_id=159,
        slug=slug,
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result="green",
        blast_class="logic",
        host_kind="github",
    )


class TestVerdictSlug:
    def test_an_owner_repo_clear_slug_never_probes_the_forge(self) -> None:
        with patch(_RECONCILE) as reconcile:
            assert _verdict_slug(_request("souliane/teatree"), "souliane/teatree") == "souliane/teatree"
        reconcile.assert_not_called()

    def test_a_workstream_slug_is_reconciled_to_the_repo_the_merge_binds(self) -> None:
        with patch(_RECONCILE, return_value="downstream-org/overlay") as reconcile:
            resolved = _verdict_slug(_request("merge-candidate-working-repos"), "souliane/teatree")
        assert resolved == "downstream-org/overlay"
        assert reconcile.call_args.kwargs["initial_slug"] == "souliane/teatree"
        assert reconcile.call_args.kwargs["reviewed_sha"] == _SHA

    def test_a_refused_reconcile_keeps_the_initial_slug(self) -> None:
        # Issuance must never become STRICTER than the merge gate it feeds; the merge's
        # own SHA bind still refuses a genuinely moved head.
        with patch(_RECONCILE, side_effect=MergePreconditionError("head moved")):
            assert _verdict_slug(_request("merge-candidate-working-repos"), "souliane/teatree") == "souliane/teatree"


_PR_ID = 159
_CLONE_ORIGIN = "souliane/teatree"
_RECOVERED = "downstream-org/downstream-overlay"
_STALE_SHA = "c" * 40
_WORKSTREAM_SLUG = "merge-candidate-working-repos"


def _gh_head_by_repo(argv: list[str]) -> tuple[int, str, str]:
    """A ``gh`` stub whose PR #159 carries the reviewed SHA only in ``_RECOVERED``.

    The clone-origin repo exposes an unrelated same-numbered PR at ``_STALE_SHA``,
    which is the #1335 shape: the initially-resolved repo answers, just about the
    wrong PR.
    """
    joined = " ".join(argv)
    return (0, _SHA if _RECOVERED in joined else _STALE_SHA, "")


class TestClearCommandReconcilesTheVerdictSlug(TestCase):
    """``ticket clear`` — not just the helper — keys the verdict where the merge reads it.

    ``_verdict_slug`` is only a fix if issuance calls it: dropping the one call site
    reverts #1335 whole, leaving the by-product verdict under the clone's ``origin``
    while the merge gate queries the recovered repo. So the binding is asserted end to
    end through ``call_command``, with only the ``gh`` subprocess and the overlay-registry
    enumeration (both machine-dependent) stubbed.
    """

    def _issue_clear(self) -> ClearIssueResult:
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_head_by_repo),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_CLONE_ORIGIN, _RECOVERED],
            ),
            patch(
                "teatree.core.management.commands.ticket.resolve_pr_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
        ):
            return cast(
                "ClearIssueResult",
                call_command(
                    "ticket",
                    "clear",
                    _PR_ID,
                    _WORKSTREAM_SLUG,
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    forge="github",
                    blast_class="docs",
                ),
            )

    def test_the_recorded_verdict_is_keyed_to_the_repo_the_merge_binds(self) -> None:
        result = self._issue_clear()

        assert result.get("issued") is True, result
        verdict = ReviewVerdict.objects.get(pr_id=_PR_ID)
        assert verdict.slug == _RECOVERED, (
            f"the CLEAR's by-product verdict must be keyed under the reconciled repo the merge "
            f"gate queries, not the initially-resolved {_CLONE_ORIGIN!r}; got {verdict.slug!r}"
        )

    def test_the_clear_row_keeps_the_workstream_slug_it_was_issued_with(self) -> None:
        # The reconcile targets the verdict key alone — the CLEAR itself still records
        # the workstream slug the orchestrator issued it under.
        self._issue_clear()

        assert MergeClear.objects.get(pr_id=_PR_ID).slug == _WORKSTREAM_SLUG
