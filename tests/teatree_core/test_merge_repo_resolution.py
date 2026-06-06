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
from teatree.core.merge_execution import (
    _GIT_BRANCH_PREFIXES,
    MergePreconditionError,
    merge_ticket_pr,
    resolve_pr_repo_slug,
)
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
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh),
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
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=self._gh_keyed_by_repo(calls)),
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


class TestGitBranchPrefixSlugNotMistakenAsOwnerRepo(TestCase):
    """Git-branch-shaped slugs must not short-circuit repo resolution (#1005).

    Before this fix ``_looks_like_owner_repo`` returned True for any string
    containing a single ``/``, so a CLEAR carrying a branch name like
    ``fix/review-cli-django-bootstrap`` was treated as
    ``owner=fix / repo=review-cli-django-bootstrap`` — bypassing the
    ticket-issue-url and clone-origin fallbacks that would have returned
    the real repo ``souliane/teatree``. The result was the opaque
    "could not resolve the live head SHA" escalation on every §17.4 merge
    whose CLEAR slug happened to be the git branch name.

    A real GitHub owner cannot be one of the standard git-branch
    namespaces (``fix``, ``feat``, ``feature``, ``chore``, ``docs``,
    ``bugfix``, ``hotfix``, ``release``, ``refactor``, ``test``, ``ci``,
    ``build``, ``perf``, ``style``) or the user's personal workflow
    prefixes (``ac``, ``wip``, ``dev``, ``tmp``), so any slug whose first
    path segment matches that set (case-insensitive) is a branch name,
    not an ``owner/repo``. The covered set is sourced from
    :data:`_GIT_BRANCH_PREFIXES` so adding a new prefix to the production
    frozenset automatically extends this test's coverage.
    """

    _BRANCH_SLUG = "fix/review-cli-django-bootstrap"

    def _branch_slug_clear(self, slug: str, pr_id: int = 1004) -> MergeClear:
        """A CLEAR whose ``slug`` is the git branch name (the #1005 bug shape).

        ``pr_id`` is used both as the PR number and to derive a unique
        ``issue_url`` so callers iterating over many slugs in subTests don't
        hit the ``issue_url`` UNIQUE constraint between iterations.
        """
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/souliane/teatree/issues/{pr_id}",
            state=Ticket.State.IN_REVIEW,
        )
        return MergeClear.objects.create(
            ticket=ticket,
            pr_id=pr_id,
            slug=slug,
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

    def test_fix_prefixed_branch_slug_resolves_real_repo_not_itself(self) -> None:
        clear = self._branch_slug_clear(self._BRANCH_SLUG)

        with patch(
            "teatree.core.merge_execution._project_repo_slug",
            return_value="souliane/teatree",
        ):
            resolved = resolve_pr_repo_slug(clear)

        assert resolved == "souliane/teatree", (
            f"branch-shaped slug {self._BRANCH_SLUG!r} must not be parsed as owner/repo; got {resolved!r}"
        )

    def test_every_common_git_branch_prefix_falls_through_to_real_repo(self) -> None:
        """All standard git-branch namespaces must fall through to the real repo.

        Parameterised in one test (rather than ``pytest.mark.parametrize``)
        because ``django.test.TestCase`` doesn't compose with pytest
        parametrize cleanly — but each case carries its own subTest label
        so failures point at the offending prefix.

        Sources the prefix set directly from
        :data:`_GIT_BRANCH_PREFIXES` so adding a new entry to the
        production frozenset automatically extends this test's coverage —
        no parallel hardcoded tuple to drift.
        """
        for index, prefix in enumerate(sorted(_GIT_BRANCH_PREFIXES)):
            with self.subTest(prefix=prefix):
                slug = f"{prefix}/some-workstream-name"
                # Each subTest needs a fresh ticket+CLEAR; offset pr_id to keep
                # the per-ticket UNIQUE issue_url constraint satisfied.
                clear = self._branch_slug_clear(slug, pr_id=2000 + index)
                with patch(
                    "teatree.core.merge_execution._project_repo_slug",
                    return_value="souliane/teatree",
                ):
                    assert resolve_pr_repo_slug(clear) == "souliane/teatree"

    def test_user_workflow_prefixes_are_covered(self) -> None:
        """The user's personal-workflow prefixes must be in the rejected set.

        ``ac/`` (the user's initials) and ``wip/``/``dev/``/``tmp/`` are the
        prefixes the user's branches actually carry — when missing from
        :data:`_GIT_BRANCH_PREFIXES` a CLEAR whose slug is the branch name
        (e.g. ``ac/cli-bundle-…``) was misparsed as ``owner=ac``, breaking
        the §17.4 merge with the opaque "could not resolve the live head"
        error. Pin them explicitly so a future refactor that "cleans up
        non-standard" prefixes from the frozenset cannot regress this.
        """
        for prefix in ("ac", "wip", "dev", "tmp"):
            with self.subTest(prefix=prefix):
                assert prefix in _GIT_BRANCH_PREFIXES

    def test_uppercase_branch_prefix_also_rejected(self) -> None:
        """Branch-prefix detection is case-insensitive — ``Fix/X`` is still a branch."""
        clear = self._branch_slug_clear("Fix/Some-Branch")

        with patch(
            "teatree.core.merge_execution._project_repo_slug",
            return_value="souliane/teatree",
        ):
            assert resolve_pr_repo_slug(clear) == "souliane/teatree"

    def test_non_branch_owner_repo_still_passes_through(self) -> None:
        """Regression guard: tightening must not break legitimate ``owner/repo`` slugs.

        ``souliane/teatree`` is a real GitHub owner, not a branch prefix,
        so it must still short-circuit straight through ``_looks_like_owner_repo``.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=1,
            slug="downstream-org/downstream-overlay",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )
        assert resolve_pr_repo_slug(clear) == "downstream-org/downstream-overlay"
