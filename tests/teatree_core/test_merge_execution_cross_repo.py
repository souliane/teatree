"""Cross-repo PR resolution — probe candidate repos on SHA mismatch (#1335).

A CLEAR issued from the teatree clone for a PR in a DIFFERENT GitHub repo
(a downstream overlay's repo, e.g. ``downstream-org/downstream-overlay#159``)
used to resolve to the running clone's ``origin`` (``souliane/teatree``)
whenever the CLEAR carried no ticket and its slug wasn't ``owner/repo``-
shaped. The engine then fetched the head SHA of an unrelated PR with the
same number in ``souliane/teatree``, and the §17.4.3 SHA-bind step raised
"PR head moved" — the CLEAR went stale on a SHA mismatch, even though a
different registered overlay's repo did own the right PR at the reviewed
SHA. The concrete downstream repo name is not load-bearing (BLUEPRINT § 1).

These tests pin Path 1 from #1335: when the resolved repo's PR #N head SHA
does NOT match ``reviewed_sha``, the engine probes other candidate repos
(every registered overlay's ``project_path`` ``origin`` plus the running
clone's ``origin``) and picks the one whose ``pulls/<N>`` head SHA matches.
If no candidate matches, the engine raises a clear error naming every
candidate it considered.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import OverlayEntry
from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeClear

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_RIGHT_SHA = "a" * 40  # the reviewed SHA on the cross-repo PR
_WRONG_SHA = "b" * 40  # the unrelated same-numbered PR on the clone origin
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'

_CLONE_ORIGIN = "souliane/teatree"
_OVERLAY_REPO = "downstream-org/downstream-overlay"


def _cross_repo_probe(joined: str, *, head: str) -> tuple[int, str, str] | None:
    """The §17.4.3 read-only probes (head / draft / checks / branch-protection).

    The branch-protection required-context set is reported EMPTY (no gate) so a
    green rollup stays green; returns ``None`` when *joined* is none of them.
    """
    if "headRefOid" in joined:
        return (0, head, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "baseRefName" in joined:
        return (0, "main", "")
    if "required_status_checks" in joined:
        return (0, '{"contexts": []}', "")
    return None


def _cross_repo_clear() -> MergeClear:
    """A CLEAR shaped like CLEAR 22 from the #1335 incident.

    No ``ticket`` (reusing the §872 ``ticketless_clear_falls_through`` path
    in :func:`resolve_pr_repo_slug`), a workstream-shaped slug, and a
    ``pr_id`` whose number happens to exist as an unrelated PR in the
    running clone's ``origin``.
    """
    return MergeClear.objects.create(
        ticket=None,
        pr_id=159,
        slug="fix-ensure-pr-default-branch-153",
        reviewed_sha=_RIGHT_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )


def _gh_keyed_by_repo(calls: list[list[str]], right_repo: str = _OVERLAY_REPO):
    """``gh`` stub keyed by ``--repo`` argument.

    The right repo's PR head == ``_RIGHT_SHA``; every other repo's PR head
    is ``_WRONG_SHA``. Which head the precondition sees is decided purely
    by which repo the sanctioned path targets.
    """

    def _gh(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        joined = " ".join(argv)
        repo = argv[argv.index("--repo") + 1] if "--repo" in argv else ""
        head = _RIGHT_SHA if repo == right_repo else _WRONG_SHA
        if (probe := _cross_repo_probe(joined, head=head)) is not None:
            return probe
        if "state,mergeCommit" in joined:
            return (0, '{"state": "OPEN", "mergeCommit": null}', "")
        if "pulls" in joined and "merge" in joined:
            return (0, '{"sha": "merged0deadbeef"}', "")
        return (0, "", "")

    return _gh


class TestCrossRepoCandidateProbe(TestCase):
    """#1335: probe candidate overlay repos when the clone-origin SHA doesn't match.

    The fix path Path 1 from the issue: detect mismatch on the resolved
    repo (``_project_repo_slug()``), then probe every candidate
    ``owner/repo`` derivable from the registered overlay project paths,
    and pick the one whose PR head SHA matches ``reviewed_sha``.
    """

    def test_probe_finds_overlay_repo_when_clone_origin_pr_is_unrelated(self) -> None:
        clear = _cross_repo_clear()
        calls: list[list[str]] = []
        candidate_entries = [
            OverlayEntry(name="downstream", overlay_class="", project_path=Path("/clones/downstream-overlay")),
        ]

        def _remote_slug_for_path(repo: str = ".", remote: str = "origin") -> str:
            del remote
            if repo == "/clones/downstream-overlay":
                return _OVERLAY_REPO
            return ""

        with (
            patch(
                "teatree.backends.forge_merge_rpc.gh_runner",
                return_value=_gh_keyed_by_repo(calls),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=candidate_entries,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.git.remote_slug",
                side_effect=_remote_slug_for_path,
            ),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert outcome.merged_sha
        assert outcome.slug == _OVERLAY_REPO, (
            f"engine must pick the candidate repo whose PR head matches reviewed_sha; got slug={outcome.slug!r}"
        )
        # The merge call (`gh api ... pulls/159/merge`) MUST target the overlay repo,
        # never the clone origin.
        merge_endpoints = [
            arg
            for argv in calls
            if "merge" in " ".join(argv) and "pulls" in " ".join(argv)
            for arg in argv
            if "pulls/" in arg
        ]
        assert merge_endpoints
        assert all(_OVERLAY_REPO in endpoint for endpoint in merge_endpoints), (
            f"merge endpoint targeted the wrong repo: {merge_endpoints}"
        )

    def test_no_candidate_match_raises_with_candidates_named(self) -> None:
        """When every candidate's PR head SHA misses, the error names them.

        The opaque "PR head moved" message was the original #1335 symptom:
        the agent had no way to tell whether the SHA mismatch was a real
        force-push or the clone-origin same-numbered confusion. The new
        error must list every candidate repo considered so the diagnosis
        is unambiguous.
        """
        clear = _cross_repo_clear()
        candidate_entries = [
            OverlayEntry(name="downstream", overlay_class="", project_path=Path("/clones/downstream-overlay")),
        ]

        # No repo returns _RIGHT_SHA — both clone-origin and overlay return _WRONG_SHA.
        def _gh_all_wrong(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _WRONG_SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            return (0, "", "")

        def _remote_slug_for_path(repo: str = ".", remote: str = "origin") -> str:
            del remote
            if repo == "/clones/downstream-overlay":
                return _OVERLAY_REPO
            return ""

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_all_wrong),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=candidate_entries,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.git.remote_slug",
                side_effect=_remote_slug_for_path,
            ),
            pytest.raises(MergePreconditionError) as exc,
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        message = str(exc.value)
        assert _CLONE_ORIGIN in message
        assert _OVERLAY_REPO in message
        assert "candidate" in message.lower()

    def test_empty_initial_live_falls_through_to_cross_repo_probe(self) -> None:
        """#1335: an empty head on the initial (wrong) repo still probes candidates.

        The cross-repo trap (CLEAR 248 / #1335): the CLEAR resolves to the
        running clone's ``origin`` (the wrong repo) for a PR that actually
        lives in a downstream overlay's repo. The clone-origin repo has no
        PR #N at all, so the forge returns an EMPTY head SHA for it — not a
        mismatching one. The buggy early-return treated empty as a transient
        auth/network failure and returned the wrong initial slug, never
        reaching the probe. The fix must fall through: the overlay repo's
        PR #N head matches ``reviewed_sha``, so the probe recovers it.
        """
        clear = _cross_repo_clear()
        calls: list[list[str]] = []
        candidate_entries = [
            OverlayEntry(name="downstream", overlay_class="", project_path=Path("/clones/downstream-overlay")),
        ]

        def _gh_empty_on_clone_origin(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            joined = " ".join(argv)
            repo = argv[argv.index("--repo") + 1] if "--repo" in argv else ""
            # The clone-origin repo has NO PR #159 -> empty head; only the
            # overlay repo owns the reviewed PR at _RIGHT_SHA.
            head = _RIGHT_SHA if repo == _OVERLAY_REPO else ""
            if (probe := _cross_repo_probe(joined, head=head)) is not None:
                return probe
            if "state,mergeCommit" in joined:
                return (0, '{"state": "OPEN", "mergeCommit": null}', "")
            if "pulls" in joined and "merge" in joined:
                return (0, '{"sha": "merged0deadbeef"}', "")
            return (0, "", "")

        def _remote_slug_for_path(repo: str = ".", remote: str = "origin") -> str:
            del remote
            if repo == "/clones/downstream-overlay":
                return _OVERLAY_REPO
            return ""

        with (
            patch(
                "teatree.backends.forge_merge_rpc.gh_runner",
                return_value=_gh_empty_on_clone_origin,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=candidate_entries,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.git.remote_slug",
                side_effect=_remote_slug_for_path,
            ),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert outcome.merged_sha
        assert outcome.slug == _OVERLAY_REPO, (
            f"empty head on the initial repo must fall through to the cross-repo probe; got slug={outcome.slug!r}"
        )
        merge_endpoints = [
            arg
            for argv in calls
            if "merge" in " ".join(argv) and "pulls" in " ".join(argv)
            for arg in argv
            if "pulls/" in arg
        ]
        assert merge_endpoints
        assert all(_OVERLAY_REPO in endpoint for endpoint in merge_endpoints), (
            f"merge endpoint targeted the wrong repo: {merge_endpoints}"
        )

    def test_resolved_repo_matches_skip_probe(self) -> None:
        """Happy path: when the resolved repo's PR matches, no probe runs.

        The probe is the recovery path for the cross-repo mismatch; in the
        common case the resolved repo IS the right repo and the merge
        proceeds with no extra ``gh`` calls.
        """
        clear = _cross_repo_clear()
        calls: list[list[str]] = []
        # The clone origin owns the matching SHA — no need to probe overlays.
        candidate_entries = [
            OverlayEntry(name="downstream", overlay_class="", project_path=Path("/clones/downstream-overlay")),
        ]

        def _remote_slug_for_path(repo: str = ".", remote: str = "origin") -> str:
            del remote
            if repo == "/clones/downstream-overlay":
                return _OVERLAY_REPO
            return ""

        with (
            patch(
                "teatree.backends.forge_merge_rpc.gh_runner",
                return_value=_gh_keyed_by_repo(calls, right_repo=_CLONE_ORIGIN),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=candidate_entries,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.git.remote_slug",
                side_effect=_remote_slug_for_path,
            ),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert outcome.slug == _CLONE_ORIGIN
        # No overlay-repo probe call was made.
        overlay_calls = [argv for argv in calls if _OVERLAY_REPO in " ".join(argv)]
        assert not overlay_calls, f"probe must not run when resolved repo's PR head matches: {overlay_calls}"
