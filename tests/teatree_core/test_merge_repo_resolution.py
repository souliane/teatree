"""`ticket merge` must resolve the real GitHub owner/repo (#871).

`MergeClear.slug` is a *workstream* slug (e.g. ``statusline-stale-wakeup``),
not a GitHub ``owner/repo``. Before #871 every ``gh`` call in
``merge_execution`` passed ``clear.slug`` as ``--repo``, so a production
CLEAR issued via ``t3 teatree ticket clear 866 statusline-stale-wakeup …``
made ``gh pr view 866 --repo statusline-stale-wakeup`` fail, ``fetch_live_head_sha``
return ``""``, and §17.4.3 step 2 raise the opaque "could not resolve the
live head SHA". The sanctioned path could issue a CLEAR but never complete
a merge.

These tests pin: a workstream-slug CLEAR resolves the real repo from the
active overlay's primary clone git remote, so ``gh`` is invoked with the
correct ``--repo``; and an unresolvable repo fails closed with an
actionable message, not the opaque live-head escalation.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core import merge_execution
from teatree.core.merge_execution import MergePreconditionError, merge_ticket_pr, resolve_pr_repo_slug
from teatree.core.models import MergeClear, Ticket

pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


def _workstream_clear(ticket: Ticket) -> MergeClear:
    """A CLEAR exactly as `t3 teatree ticket clear` issues it in production."""
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=866,
        slug="statusline-stale-wakeup",
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )


class TestResolvePrRepoSlug(TestCase):
    def test_owner_repo_shaped_slug_passes_through(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )
        assert resolve_pr_repo_slug(clear) == "souliane/teatree"

    def test_workstream_slug_resolves_repo_from_clone_remote(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _workstream_clear(ticket)

        with patch(
            "teatree.core.merge_execution._project_repo_slug",
            return_value="souliane/teatree",
        ):
            assert resolve_pr_repo_slug(clear) == "souliane/teatree"

    def test_unresolvable_repo_fails_closed_with_actionable_message(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _workstream_clear(ticket)

        with (
            patch("teatree.core.merge_execution._project_repo_slug", return_value=""),
            pytest.raises(MergePreconditionError, match="could not resolve the GitHub repo"),
        ):
            resolve_pr_repo_slug(clear)


class TestMergeUsesResolvedRepo(TestCase):
    def test_workstream_slug_merge_calls_gh_with_real_repo(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _workstream_clear(ticket)
        calls: list[list[str]] = []

        def _gh(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "pulls" in joined and "merge" in joined:
                return (0, '{"sha": "merged0deadbeef"}', "")
            return (0, "", "")

        with (
            patch("teatree.core.merge_execution._run_gh", side_effect=_gh),
            patch(
                "teatree.core.merge_execution._project_repo_slug",
                return_value="souliane/teatree",
            ),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert outcome.merged_sha
        # Every gh invocation must target the real repo, never the workstream slug.
        for argv in calls:
            joined = " ".join(argv)
            assert "statusline-stale-wakeup" not in joined
        repo_args = [argv[argv.index("--repo") + 1] for argv in calls if "--repo" in argv]
        assert repo_args
        assert all(r == "souliane/teatree" for r in repo_args)

    def test_workstream_slug_unresolvable_repo_is_actionable_not_opaque(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _workstream_clear(ticket)

        with (
            patch("teatree.core.merge_execution._project_repo_slug", return_value=""),
            pytest.raises(MergePreconditionError) as exc,
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        message = str(exc.value)
        assert "could not resolve the GitHub repo" in message
        assert "could not resolve the live head" not in message


class TestOverlayRepoDiffersFromCloneOrigin(TestCase):
    """#931 — an overlay's GitHub repo differs from the ``t3`` clone origin.

    A sanctioned ``ticket merge`` for a PR in a downstream overlay repo
    (here ``downstream-org/downstream-overlay#139``) must bind the
    live-head check to that repo — the repo the ticket's PR belongs to —
    NOT to ``souliane/teatree`` (the running clone's ``origin``). Before
    #931 the live-head lookup resolved the clone-origin same-numbered PR
    (an unrelated ``souliane/teatree#139`` whose head differs), so the
    SHA-bind precondition saw "head moved" and every downstream-overlay
    sanctioned merge was blocked.

    The concrete repo name is not load-bearing — any ``owner/repo`` that
    is not the clone origin reproduces the bug; a neutral placeholder is
    used so core/tests stay overlay-agnostic (BLUEPRINT § 1).

    Only the ``gh`` subprocess (the network boundary) is stubbed; the
    repo resolution runs through real teatree code against a real
    ``Ticket`` row.
    """

    _OVERLAY_REPO = "downstream-org/downstream-overlay"
    _OVERLAY_SHA = "5" * 40  # overlay repo PR #139 head == reviewed SHA
    _ORIGIN_SHA = "6" * 40  # unrelated clone-origin PR #139 head (moved-on)

    def _overlay_clear(self) -> MergeClear:
        ticket = Ticket.objects.create(
            overlay="downstream",
            issue_url=f"https://github.com/{self._OVERLAY_REPO}/issues/139",
            state=Ticket.State.IN_REVIEW,
        )
        return MergeClear.objects.create(
            ticket=ticket,
            pr_id=139,
            slug="overlay-repo-differs-from-clone-origin",
            reviewed_sha=self._OVERLAY_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

    def _gh_keyed_by_repo(self, calls: list[list[str]]):
        """A ``gh`` stub whose PR head depends on the ``--repo`` argument.

        The overlay repo's PR #139 head == the reviewed SHA (mergeable);
        the clone-origin PR #139 head is a different, unrelated SHA.
        Which head the precondition sees is decided purely by which repo
        the sanctioned path targets.
        """

        def _gh(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            joined = " ".join(argv)
            repo = argv[argv.index("--repo") + 1] if "--repo" in argv else ""
            head = self._OVERLAY_SHA if repo == self._OVERLAY_REPO else self._ORIGIN_SHA
            if "headRefOid" in joined:
                return (0, head, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "pulls" in joined and "merge" in joined:
                return (0, '{"sha": "merged0deadbeef"}', "")
            return (0, "", "")

        return _gh

    def test_sha_bind_precondition_passes_against_overlay_repo(self) -> None:
        clear = self._overlay_clear()
        calls: list[list[str]] = []

        with (
            patch("teatree.core.merge_execution._run_gh", side_effect=self._gh_keyed_by_repo(calls)),
            patch("teatree.core.merge_execution._project_repo_slug", return_value="souliane/teatree"),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert outcome.merged_sha
        repo_args = [argv[argv.index("--repo") + 1] for argv in calls if "--repo" in argv]
        assert repo_args
        assert all(r == self._OVERLAY_REPO for r in repo_args), (
            f"sanctioned merge targeted the wrong repo: {sorted(set(repo_args))} "
            f"(must be the ticket's overlay repo, not the clone origin)"
        )

    def test_resolve_pr_repo_slug_prefers_ticket_issue_url_over_clone_origin(self) -> None:
        clear = self._overlay_clear()

        with patch("teatree.core.merge_execution._project_repo_slug", return_value="souliane/teatree"):
            assert resolve_pr_repo_slug(clear) == self._OVERLAY_REPO

    def test_ticketless_clear_falls_through_to_clone_origin(self) -> None:
        """A CLEAR with no ticket keeps the #872 clone-origin behaviour."""
        clear = MergeClear.objects.create(
            ticket=None,
            pr_id=139,
            slug="overlay-repo-differs-from-clone-origin",
            reviewed_sha=self._OVERLAY_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        with patch("teatree.core.merge_execution._project_repo_slug", return_value="souliane/teatree"):
            assert resolve_pr_repo_slug(clear) == "souliane/teatree"

    def test_clear_with_blank_issue_url_falls_through_to_clone_origin(self) -> None:
        """A ticket with no issue_url keeps the #872 clone-origin behaviour."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=139,
            slug="overlay-repo-differs-from-clone-origin",
            reviewed_sha=self._OVERLAY_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        with patch("teatree.core.merge_execution._project_repo_slug", return_value="souliane/teatree"):
            assert resolve_pr_repo_slug(clear) == "souliane/teatree"

    def test_clear_with_non_github_issue_url_falls_through_to_clone_origin(self) -> None:
        """A ticket whose issue_url is unparsable falls back, not crash."""
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://example.invalid/not-an-issue",
            state=Ticket.State.IN_REVIEW,
        )
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=139,
            slug="overlay-repo-differs-from-clone-origin",
            reviewed_sha=self._OVERLAY_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        with patch("teatree.core.merge_execution._project_repo_slug", return_value="souliane/teatree"):
            assert resolve_pr_repo_slug(clear) == "souliane/teatree"


class TestProjectRepoSlugHelper(TestCase):
    def test_project_repo_slug_uses_project_root_git_remote(self) -> None:
        with (
            patch(
                "teatree.core.merge_execution.find_project_root",
                return_value=Path("/clone/teatree"),
            ),
            patch(
                "teatree.core.merge_execution.git.remote_slug",
                return_value="souliane/teatree",
            ) as remote_slug,
        ):
            assert merge_execution._project_repo_slug() == "souliane/teatree"
        remote_slug.assert_called_once_with(repo="/clone/teatree")

    def test_project_repo_slug_empty_when_no_project_root(self) -> None:
        with patch("teatree.core.merge_execution.find_project_root", return_value=None):
            assert merge_execution._project_repo_slug() == ""
