"""Forge merge-RPC argv + payload parsing — canonical home for the merge transport.

The 5 §17.4.3 merge-RPC methods on :class:`GitHubCodeHost` /
:class:`GitLabCodeHost` delegate to :class:`GhMergeRpc` / :class:`GlabMergeRpc`
here, each holding its forge's allow-to-fail runner. This keeps the gh and glab
argv in one focused module — the chokepoint home the argv-ban rule (#1890) will
point at — while the methods stay on the host classes so the ``CodeHostBackend``
Protocol is satisfied. Raw I/O only: every verdict / transient / head-moved
classification stays in ``teatree.core.merge_execution`` for byte-for-byte error
parity on the keystone path.
"""

import json
import os
import shutil
from collections.abc import Callable

from teatree.core.backend_protocols import ROLLUP_QUERY_FAILED, ForgeMergeResult, PrMergeState
from teatree.types import RawAPIDict

Runner = Callable[[list[str]], tuple[int, str, str]]


def gh_runner(token: str) -> Runner:
    """A ``gh`` allow-to-fail runner — auth via ``GH_TOKEN`` when *token* is set.

    Distinct from ``github._run_gh`` (which uses ``run_checked`` and raises on
    non-zero): the merge-RPC methods inspect the return code, so they need the
    ``(rc, out, err)`` shape :func:`run_allowed_to_fail` returns.
    """
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — avoid an import cycle at module load.

    def run(argv: list[str]) -> tuple[int, str, str]:
        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": token} if token else None
        result = run_allowed_to_fail([gh, *argv], expected_codes=None, env=env)
        return result.returncode, result.stdout, result.stderr

    return run


def glab_runner() -> Runner:
    """A ``glab`` allow-to-fail runner — ambient ``glab`` auth (no token plumbed).

    The §17.4.3 merge path uses the ``glab`` subprocess (not the httpx
    ``GitLabAPI`` client) so the SHA-bind behaviour and error strings match what
    the keystone tests pin.
    """
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — avoid an import cycle at module load.

    def run(argv: list[str]) -> tuple[int, str, str]:
        glab = shutil.which("glab") or "glab"
        result = run_allowed_to_fail([glab, *argv], expected_codes=None)
        return result.returncode, result.stdout, result.stderr

    return run


def glab_project_path(slug: str) -> str:
    """URL-encode a project slug for ``glab api projects/<encoded>/...``.

    GitLab's REST API requires the project identifier ``group/repo`` (or
    ``group/subgroup/repo``) to be URL-encoded — the slashes become ``%2F``.
    """
    return slug.replace("/", "%2F")


class GhMergeRpc:
    """GitHub ``gh`` merge-RPC argv + payload parsing — raw I/O for one host."""

    def __init__(self, run: Runner) -> None:
        self._run = run

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "headRefOid", "--jq", ".headRefOid"],
        )
        return out.strip() if rc == 0 else ""

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        rc, out, _ = self._run(["pr", "view", str(pr_id), "--repo", slug, "--json", "state,mergeCommit"])
        if rc != 0 or not out.strip():
            return PrMergeState(state="", merge_commit_oid="")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return PrMergeState(state="", merge_commit_oid="")
        if not isinstance(data, dict):
            return PrMergeState(state="", merge_commit_oid="")
        state = str(data.get("state") or "")
        merge_commit = data.get("mergeCommit")
        oid = str(merge_commit.get("oid") or "") if isinstance(merge_commit, dict) else ""
        return PrMergeState(state=state, merge_commit_oid=oid)

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "isDraft", "--jq", ".isDraft"],
        )
        return rc == 0 and out.strip().lower() == "true"

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        rc, out, _ = self._run(
            [
                "pr",
                "view",
                str(pr_id),
                "--repo",
                slug,
                "--json",
                "statusCheckRollup",
                "--jq",
                ".statusCheckRollup",
            ],
        )
        if rc != 0:
            return [ROLLUP_QUERY_FAILED]
        try:
            rollup = json.loads(out) if out.strip() else []
        except json.JSONDecodeError:
            return [ROLLUP_QUERY_FAILED]
        if not isinstance(rollup, list):
            return [ROLLUP_QUERY_FAILED]
        return [entry for entry in rollup if isinstance(entry, dict)]

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> ForgeMergeResult:
        endpoint = f"repos/{slug}/pulls/{pr_id}/merge"
        rc, out, err = self._run(
            ["api", "--method", "PUT", endpoint, "-f", "merge_method=squash", "-f", f"sha={expected_head_oid}"],
        )
        merged_sha = ""
        if rc == 0:
            try:
                merged = json.loads(out) if out.strip() else {}
            except json.JSONDecodeError:
                merged = {}
            merged_sha = str(merged.get("sha") or "") if isinstance(merged, dict) else ""
        return ForgeMergeResult(returncode=rc, stdout=out, stderr=err, merged_sha=merged_sha)


class GlabMergeRpc:
    """GitLab ``glab`` merge-RPC argv + payload parsing — raw I/O for one host."""

    def __init__(self, run: Runner) -> None:
        self._run = run

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}"])
        if rc != 0 or not out.strip():
            return ""
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("sha") or "")

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}"])
        if rc != 0 or not out.strip():
            return PrMergeState(state="", merge_commit_oid="")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return PrMergeState(state="", merge_commit_oid="")
        if not isinstance(data, dict):
            return PrMergeState(state="", merge_commit_oid="")
        state = str(data.get("state") or "").upper()  # "merged" → "MERGED" (parity with GitHub)
        oid = str(data.get("merge_commit_sha") or data.get("squash_commit_sha") or "")
        return PrMergeState(state=state, merge_commit_oid=oid)

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}"])
        if rc != 0 or not out.strip():
            return False
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict):
            return False
        # ``draft`` is canonical on modern GitLab; ``work_in_progress`` is the legacy
        # field kept for compatibility — accept either.
        return bool(data.get("draft") or data.get("work_in_progress"))

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}/pipelines"])
        if rc != 0:
            return [ROLLUP_QUERY_FAILED]
        try:
            pipelines = json.loads(out) if out.strip() else []
        except json.JSONDecodeError:
            return [ROLLUP_QUERY_FAILED]
        if not isinstance(pipelines, list):
            return [ROLLUP_QUERY_FAILED]
        return [entry for entry in pipelines if isinstance(entry, dict)]

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> ForgeMergeResult:
        endpoint = f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}/merge"
        rc, out, err = self._run(["api", "-X", "PUT", endpoint, "-f", f"sha={expected_head_oid}", "-f", "squash=true"])
        merged_sha = ""
        if rc == 0:
            try:
                merged = json.loads(out) if out.strip() else {}
            except json.JSONDecodeError:
                merged = {}
            if isinstance(merged, dict):
                merged_sha = str(merged.get("merge_commit_sha") or merged.get("sha") or "")
        return ForgeMergeResult(returncode=rc, stdout=out, stderr=err, merged_sha=merged_sha)
