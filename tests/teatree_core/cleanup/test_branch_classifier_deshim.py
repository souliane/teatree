"""The branch classifier lives ONLY in ``worktree.branch_classification`` (no shim).

Guards the #2609-era de-shim: the subject classifier and the content gate were
consolidated into :mod:`teatree.core.worktree.branch_classification`, and the
back-compat re-export shim on :mod:`teatree.core.cleanup.cleanup` was deleted
(no-deprecated-aliases rule). This test fails if any of the re-export shim
creeps back, and pins the "signals-never-authorizes" rename of the subject
pre-filter.
"""

from teatree.core.cleanup import cleanup as cleanup_mod
from teatree.core.worktree import branch_classification


class TestBranchClassifierIsCanonicalOnly:
    """The classifier API is reachable only from its canonical module."""

    def test_cleanup_does_not_re_export_the_subject_prefilter(self) -> None:
        """``cleanup.cleanup`` no longer re-exports the branch classifier surface.

        The management commands and sync backends must import the classifier
        from :mod:`teatree.core.worktree.branch_classification` directly; the
        old ``from teatree.core.cleanup.cleanup import ...`` seam is gone.
        """
        for shimmed in (
            "SubjectPrefilterResult",
            "BranchClassification",
            "BranchCommit",
            "prefilter_branch_commits_by_subject",
            "classify_branch_commits",
            "probe_host_cli",
            "_pr_merge_commit_sha",
        ):
            assert not hasattr(cleanup_mod, shimmed), (
                f"cleanup.cleanup must not re-export {shimmed!r} — import it from "
                "teatree.core.worktree.branch_classification"
            )

    def test_cleanup_all_lists_only_its_own_surface(self) -> None:
        """``cleanup.__all__`` carries none of the branch-classifier names."""
        classifier_surface = {
            "SubjectPrefilterResult",
            "BranchClassification",
            "BranchCommit",
            "prefilter_branch_commits_by_subject",
            "classify_branch_commits",
            "probe_host_cli",
            "_pr_merge_commit_sha",
            "content_equivalence_blockers",
        }
        assert classifier_surface.isdisjoint(set(cleanup_mod.__all__))

    def test_subject_prefilter_is_renamed_to_signal_never_authorizes(self) -> None:
        """The subject engine is ``prefilter_*`` — the old ``classify_*`` name is gone."""
        assert hasattr(branch_classification, "prefilter_branch_commits_by_subject")
        assert hasattr(branch_classification, "SubjectPrefilterResult")
        assert not hasattr(branch_classification, "classify_branch_commits")
        assert not hasattr(branch_classification, "BranchClassification")

    def test_dead_content_upstream_boolean_view_is_removed(self) -> None:
        """The zero-caller ``branch_content_upstream`` public API is deleted."""
        assert not hasattr(branch_classification, "branch_content_upstream")
