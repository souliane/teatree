"""Merge-candidate enumeration must include overlay working-repos (#2323).

The #1335 cross-repo probe enumerated only (1) the running clone's ``origin``
and (2) each registered overlay's *package* repo (``project_path`` ``origin``).
An overlay also operates on **working-repos** it does not package — e.g. an
``e2e`` companion repo. A green, cold-reviewed PR living in such a working-repo
could not be bound by ``t3 <overlay> ticket merge``: the candidate set never
contained that repo, so the probe found no candidate carrying the reviewed SHA
and the merge escalated "PR head moved / no candidate carries that SHA".

The fix adds an optional ``OverlayBase.get_merge_candidate_repo_slugs()`` hook
(default ``[]``) declaring an overlay's working-repos as ``owner/repo`` slugs;
``_iter_candidate_repo_slugs`` appends them to the candidate set (normalizing
SSH / HTTPS / host-alias URL forms up to ``owner/repo``), preserving the
best-effort contract: a per-overlay failure is swallowed.

These tests pin symmetric coverage:

- a working-repo PR at the reviewed SHA RESOLVES via the candidate probe;
- a foreign same-numbered PR at a DIFFERENT SHA still does NOT resolve, preserving the #1335 cross-repo-confusion guard;
- a per-overlay enumeration failure is swallowed and the candidate set is otherwise intact.

The concrete working-repo name is a neutral placeholder — core/tests stay
overlay-agnostic (BLUEPRINT § 1). Only the ``gh`` subprocess (the network
boundary) is stubbed; the candidate enumeration runs through real teatree code.
"""

from pathlib import Path
from typing import override
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import OverlayEntry
from teatree.core.merge import MergePreconditionError, merge_ticket_pr, pr_slug_resolution
from teatree.core.models import MergeClear
from teatree.core.overlay import OverlayBase

pytestmark = pytest.mark.django_db

_RIGHT_SHA = "a" * 40  # the reviewed SHA on the working-repo PR
_WRONG_SHA = "b" * 40  # the unrelated same-numbered PR on the clone origin
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'

_CLONE_ORIGIN = "souliane/teatree"
# A working-repo the overlay operates on but does NOT package (no project_path).
# Declared via the new hook in its canonical host-alias SSH form to exercise
# slug normalization end-to-end.
_WORKING_REPO_SLUG = "downstream-org/downstream-overlay-e2e"
_WORKING_REPO_SSH = f"git@github.com-alias:{_WORKING_REPO_SLUG}.git"


class _WorkingRepoOverlay(OverlayBase):
    """A minimal overlay declaring one working-repo via the new merge hook.

    ``get_repos`` / ``get_provision_steps`` satisfy the ABC but are irrelevant
    to candidate enumeration; the working-repo is declared ONLY through
    :meth:`get_merge_candidate_repo_slugs`, in its host-alias SSH URL form, so
    the test proves the enumeration normalizes it up to ``owner/repo``.
    """

    @override
    def get_repos(self) -> list[str]:
        return ["downstream-overlay"]

    @override
    def get_provision_steps(self, worktree: object) -> list:
        return []

    @override
    def get_merge_candidate_repo_slugs(self) -> list[str]:
        return [_WORKING_REPO_SSH]


class _ExplodingOverlay(OverlayBase):
    """An overlay whose merge-candidate hook RAISES — must be swallowed."""

    @override
    def get_repos(self) -> list[str]:
        return ["broken-overlay"]

    @override
    def get_provision_steps(self, worktree: object) -> list:
        return []

    @override
    def get_merge_candidate_repo_slugs(self) -> list[str]:
        msg = "overlay enumeration blew up"
        raise RuntimeError(msg)


def _working_repo_clear() -> MergeClear:
    """A CLEAR with no ticket + workstream-shaped slug (the cross-repo shape).

    No ``ticket`` (so :func:`resolve_pr_repo_slug` falls through to the clone
    origin), a workstream-shaped slug, and a ``pr_id`` whose number exists as
    an unrelated PR in the clone origin.
    """
    return MergeClear.objects.create(
        ticket=None,
        pr_id=159,
        slug="fix-some-working-repo-change",
        reviewed_sha=_RIGHT_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )


def _gh_keyed_by_repo(calls: list[list[str]], right_repo: str):
    """``gh`` stub keyed by ``--repo`` — only *right_repo*'s PR head matches."""

    def _gh(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        joined = " ".join(argv)
        repo = argv[argv.index("--repo") + 1] if "--repo" in argv else ""
        head = _RIGHT_SHA if repo == right_repo else _WRONG_SHA
        if "headRefOid" in joined:
            return (0, head, "")
        if "isDraft" in joined:
            return (0, "false", "")
        if "statusCheckRollup" in joined:
            return (0, _GREEN, "")
        if "state,mergeCommit" in joined:
            return (0, '{"state": "OPEN", "mergeCommit": null}', "")
        if "pulls" in joined and "merge" in joined:
            return (0, '{"sha": "merged0deadbeef"}', "")
        return (0, "", "")

    return _gh


class TestWorkingRepoCandidateEnumeration(TestCase):
    """#2323: an overlay working-repo PR resolves via the candidate probe."""

    def test_iter_candidates_includes_overlay_working_repo_slug(self) -> None:
        """The enumeration appends the overlay's declared working-repo slug.

        Pins the unit-level contract directly: with no overlay declaring a
        working-repo the candidate set is just the clone origin; once an overlay
        declares one (in host-alias SSH form), the normalized ``owner/repo``
        appears in the candidate set. This is the assertion that goes RED on the
        pre-fix code (the enumeration never consults the hook).
        """
        with (
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=[],
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.get_all_overlays",
                return_value={"working": _WorkingRepoOverlay()},
            ),
        ):
            candidates = pr_slug_resolution._iter_candidate_repo_slugs()

        assert _WORKING_REPO_SLUG in candidates, (
            f"enumeration must include the overlay's working-repo slug; got {candidates!r}"
        )
        assert _CLONE_ORIGIN in candidates

    def test_probe_finds_working_repo_when_clone_origin_pr_is_unrelated(self) -> None:
        """End-to-end: a working-repo PR at the reviewed SHA merges via the probe."""
        clear = _working_repo_clear()
        calls: list[list[str]] = []

        with (
            patch(
                "teatree.backends.forge_merge_rpc.gh_runner",
                return_value=_gh_keyed_by_repo(calls, right_repo=_WORKING_REPO_SLUG),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=[],
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.get_all_overlays",
                return_value={"working": _WorkingRepoOverlay()},
            ),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert outcome.merged_sha
        assert outcome.slug == _WORKING_REPO_SLUG, (
            f"engine must pick the working-repo whose PR head matches reviewed_sha; got slug={outcome.slug!r}"
        )
        merge_endpoints = [
            arg
            for argv in calls
            if "merge" in " ".join(argv) and "pulls" in " ".join(argv)
            for arg in argv
            if "pulls/" in arg
        ]
        assert merge_endpoints
        assert all(_WORKING_REPO_SLUG in endpoint for endpoint in merge_endpoints), (
            f"merge endpoint targeted the wrong repo: {merge_endpoints}"
        )

    def test_foreign_same_numbered_pr_at_different_sha_does_not_resolve(self) -> None:
        """#1335 guard preserved: no candidate carries the SHA → fail loud.

        The working-repo's PR #159 is at a DIFFERENT (wrong) SHA, and so is the
        clone origin's same-numbered PR. The probe must NOT bind either — it
        raises a precondition error naming every candidate considered, never
        merges a same-numbered PR that does not carry the reviewed work.
        """
        clear = _working_repo_clear()

        def _gh_all_wrong(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _WRONG_SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            return (0, "", "")

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_all_wrong),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=[],
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.get_all_overlays",
                return_value={"working": _WorkingRepoOverlay()},
            ),
            pytest.raises(MergePreconditionError) as exc,
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        message = str(exc.value)
        assert _CLONE_ORIGIN in message
        assert _WORKING_REPO_SLUG in message
        assert "candidate" in message.lower()

    def test_per_overlay_enumeration_failure_is_swallowed(self) -> None:
        """A hook that raises must not poison the candidate set (best-effort).

        With one overlay's ``get_merge_candidate_repo_slugs`` raising and a
        second declaring a real working-repo, the enumeration swallows the
        failure and still yields the clone origin + the healthy overlay's slug.
        """
        with (
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_CLONE_ORIGIN,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.discover_overlays",
                return_value=[],
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution.get_all_overlays",
                return_value={"boom": _ExplodingOverlay(), "working": _WorkingRepoOverlay()},
            ),
        ):
            candidates = pr_slug_resolution._iter_candidate_repo_slugs()

        assert _CLONE_ORIGIN in candidates
        assert _WORKING_REPO_SLUG in candidates, (
            "a raising overlay hook must be swallowed without dropping healthy candidates"
        )


class TestCandidateSourceHelpersAreBestEffort(TestCase):
    """The two overlay-source helpers degrade to ``[]`` instead of propagating.

    Pins the best-effort contract per source: a failing registry lookup
    (``discover_overlays`` / ``get_all_overlays`` raising) yields nothing rather
    than aborting the whole candidate enumeration. A project-path entry with no
    ``project_path`` and a project path whose ``remote_slug`` raises are both
    skipped without dropping the rest.
    """

    def test_package_repo_slugs_empty_when_discover_overlays_raises(self) -> None:
        with patch(
            "teatree.core.merge.pr_slug_resolution.discover_overlays",
            side_effect=RuntimeError("registry down"),
        ):
            assert pr_slug_resolution._overlay_package_repo_slugs() == []

    def test_package_repo_slugs_skips_pathless_entry_and_unresolvable_remote(self) -> None:
        entries = [
            OverlayEntry(name="pathless", overlay_class="", project_path=None),
            OverlayEntry(name="broken-remote", overlay_class="", project_path=Path("/clones/no-remote")),
        ]

        def _remote_slug_raises(repo: str = ".", remote: str = "origin") -> str:
            del remote
            msg = f"no remote for {repo}"
            raise RuntimeError(msg)

        with (
            patch("teatree.core.merge.pr_slug_resolution.discover_overlays", return_value=entries),
            patch("teatree.core.merge.pr_slug_resolution.git.remote_slug", side_effect=_remote_slug_raises),
        ):
            assert pr_slug_resolution._overlay_package_repo_slugs() == []

    def test_working_repo_slugs_empty_when_get_all_overlays_raises(self) -> None:
        with patch(
            "teatree.core.merge.pr_slug_resolution.get_all_overlays",
            side_effect=RuntimeError("overlay instantiation blew up"),
        ):
            assert pr_slug_resolution._overlay_working_repo_slugs() == []


class TestNormalizeRepoSlug(TestCase):
    """``normalize_repo_slug`` canonicalizes every URL form up to ``owner/repo``.

    The placeholder ``downstream-org/downstream-overlay-e2e`` keeps the test
    overlay-agnostic (BLUEPRINT § 1) — only the URL *shape* of each form is
    under test, not any concrete repo identity.
    """

    _SLUG = "downstream-org/downstream-overlay-e2e"

    def test_bare_owner_repo_passes_through(self) -> None:
        assert pr_slug_resolution.normalize_repo_slug(self._SLUG) == self._SLUG

    def test_https_url_is_normalized(self) -> None:
        assert pr_slug_resolution.normalize_repo_slug(f"https://github.com/{self._SLUG}.git") == self._SLUG

    def test_ssh_url_is_normalized(self) -> None:
        assert pr_slug_resolution.normalize_repo_slug(f"git@github.com:{self._SLUG}.git") == self._SLUG

    def test_host_alias_ssh_url_is_normalized(self) -> None:
        assert pr_slug_resolution.normalize_repo_slug(f"git@github.com-myalias:{self._SLUG}.git") == self._SLUG

    def test_empty_and_unparseable_yield_empty(self) -> None:
        assert pr_slug_resolution.normalize_repo_slug("") == ""
        assert pr_slug_resolution.normalize_repo_slug("not-a-slug") == ""
