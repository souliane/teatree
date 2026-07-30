"""Tri-state head-branch provenance reads on both forges (#3244).

``fetch_pr_same_repo`` is the §17.4.3 fork-gate input: True = same-repo head
(trusted), False = fork / cross-repo (holds for a human), None = the forge did
not report it ⇒ the gate fails closed to the author check. GitHub reads
``isCrossRepository``; GitLab compares the MR source/target project ids — the
seam that makes overlay MRs cross the same gate.
"""

import json

from teatree.backends.forge_merge_rpc import GhMergeRpc, GlabMergeRpc

_SLUG = "souliane/teatree"
_PR_ID = 42


def _gh(answer: str, *, rc: int = 0) -> GhMergeRpc:
    def _run(argv: list[str]) -> tuple[int, str, str]:
        assert "isCrossRepository" in " ".join(argv)
        return (rc, answer, "")

    return GhMergeRpc(_run)


def _glab(body: object, *, rc: int = 0) -> GlabMergeRpc:
    def _run(argv: list[str]) -> tuple[int, str, str]:
        return (rc, json.dumps(body) if rc == 0 else "", "")

    return GlabMergeRpc(_run)


class TestGitHubProvenance:
    def test_cross_repository_true_is_fork(self) -> None:
        assert _gh("true").fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is False

    def test_cross_repository_false_is_same_repo(self) -> None:
        assert _gh("false").fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is True

    def test_empty_answer_is_unknown(self) -> None:
        assert _gh("").fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is None

    def test_forge_error_is_unknown(self) -> None:
        assert _gh("true", rc=1).fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is None


class TestGitLabProvenance:
    def test_same_project_ids_is_same_repo(self) -> None:
        rpc = _glab({"source_project_id": 7, "target_project_id": 7})
        assert rpc.fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is True

    def test_distinct_project_ids_is_fork(self) -> None:
        rpc = _glab({"source_project_id": 9, "target_project_id": 7})
        assert rpc.fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is False

    def test_missing_project_ids_is_unknown(self) -> None:
        rpc = _glab({"iid": 42})
        assert rpc.fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is None

    def test_forge_error_is_unknown(self) -> None:
        rpc = _glab({}, rc=1)
        assert rpc.fetch_pr_same_repo(slug=_SLUG, pr_id=_PR_ID) is None
