"""The CLEAR's by-product verdict is keyed where the MERGE will look for it.

``resolve_pr_repo_slug`` is only half of the keystone's resolution; the other half is
the #1335 cross-repo reconcile. Issuance ran only the first half, so a CLEAR for a
downstream overlay's PR recorded its verdict under the running clone's ``origin`` while
the merge queried the recovered repo — a CLEAR unmergeable for its whole life, with
re-issuing reproducing the same wrong slug.
"""

from unittest.mock import patch

from teatree.core.management.commands.ticket import _verdict_slug
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models import ClearRequest

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
