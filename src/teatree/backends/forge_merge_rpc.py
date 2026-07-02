"""Forge merge-RPC argv + payload parsing — canonical home for the merge transport.

The 5 §17.4.3 merge-RPC methods on :class:`GitHubCodeHost` /
:class:`GitLabCodeHost` delegate to :class:`GhMergeRpc` / :class:`GlabMergeRpc`
here, each holding its forge's allow-to-fail runner. This keeps the gh and glab
argv in one focused module — the chokepoint home the argv-ban rule (#1890) will
point at — while the methods stay on the host classes so the ``CodeHostBackend``
Protocol is satisfied. Raw I/O only: every verdict / transient / head-moved
classification stays in ``teatree.core.merge.execution`` for byte-for-byte error
parity on the keystone path.
"""

import json
import os
import shutil
from collections.abc import Callable

from teatree.core.backend_protocols import ROLLUP_QUERY_FAILED, ForgeMergeResult, PrMergeState
from teatree.types import RawAPIDict
from teatree.utils.run import run_allowed_to_fail

Runner = Callable[[list[str]], tuple[int, str, str]]


def gh_runner(token: str) -> Runner:
    """A ``gh`` allow-to-fail runner — auth via ``GH_TOKEN`` when *token* is set.

    Distinct from ``github._run_gh`` (which uses ``run_checked`` and raises on
    non-zero): the merge-RPC methods inspect the return code, so they need the
    ``(rc, out, err)`` shape :func:`run_allowed_to_fail` returns.
    """

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

    def fetch_pr_author(self, *, slug: str, pr_id: int) -> str:
        """The PR author ``login`` — the §17.4.3 author-gate input (#1773).

        Returns ``""`` on any error; the empty author is fail-closed at the
        keystone (an author that cannot be proved trusted does not auto-merge
        on a public repo).
        """
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "author", "--jq", ".author.login"],
        )
        return out.strip() if rc == 0 else ""

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

    def fetch_required_status_check_contexts(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        """Branch-protection required-status-check contexts for the PR's base branch.

        The AUTHORITATIVE required set for §17.4.3 step 3: only a context the
        repo's branch protection lists as a required status check may block the
        merge. A check NOT in this set (``eval``, advisory lanes, …) never blocks
        regardless of its conclusion.

        Returns ``[ROLLUP_QUERY_FAILED]`` (fail CLOSED) when the base branch or
        the ``branches/<base>/protection/required_status_checks`` endpoint cannot
        be read — an indeterminate required set must refuse the merge, never fall
        open. Returns ``[]`` when the base branch genuinely has no required-status-
        check protection (the determinate "no required gate" 404 the forge raises
        with a ``Branch not protected`` / ``Required status checks not enabled``
        body — no gate → green). Otherwise one ``{"context": <name>}`` entry per
        required context, the UNION of the legacy ``contexts`` array and the newer
        ``checks[].context`` array (GitHub returns both; either may carry a name).
        """
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "baseRefName", "--jq", ".baseRefName"],
        )
        base = out.strip()
        if rc != 0 or not base:
            return [ROLLUP_QUERY_FAILED]
        rc, out, err = self._run(["api", f"repos/{slug}/branches/{base}/protection/required_status_checks"])
        if rc != 0:
            body = f"{out}\n{err}".lower()
            # `base` was already confirmed a real branch above, so a 404 here
            # can only mean "no protection configured" — never "no such branch".
            if "branch not protected" in body or "required status checks not enabled" in body or "not found" in body:
                return []
            return [ROLLUP_QUERY_FAILED]
        try:
            data = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            return [ROLLUP_QUERY_FAILED]
        if not isinstance(data, dict):
            return [ROLLUP_QUERY_FAILED]
        contexts: set[str] = set()
        for ctx in data.get("contexts") or []:
            if isinstance(ctx, str) and ctx:
                contexts.add(ctx)
        for check in data.get("checks") or []:
            if isinstance(check, dict) and isinstance(check.get("context"), str) and check["context"]:
                contexts.add(check["context"])
        return [{"context": ctx} for ctx in sorted(contexts)]

    def fetch_pr_changed_paths(self, *, slug: str, pr_id: int) -> list[str]:
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "files", "--jq", ".files[].path"],
        )
        if rc != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

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

    def fetch_pr_author(self, *, slug: str, pr_id: int) -> str:
        """The MR author ``username`` — the §17.4.3 author-gate input (#1773).

        Returns ``""`` on any error; the empty author is fail-closed at the
        keystone (an author that cannot be proved trusted does not auto-merge
        on a public repo).
        """
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}"])
        if rc != 0 or not out.strip():
            return ""
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, dict):
            return ""
        author = data.get("author")
        return str(author.get("username") or "") if isinstance(author, dict) else ""

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

    @staticmethod
    def fetch_required_status_check_contexts(*, slug: str, pr_id: int) -> list[RawAPIDict]:
        """GitLab has no branch-protection-required-status-checks gate on this path.

        The GitLab §17.4.3 verdict is the head pipeline's overall status (see
        :func:`core.merge.ci_rollup._classify_gitlab_pipeline`), which already
        aggregates the required jobs server-side. Core never calls this on the
        GitLab path; the method exists only to satisfy the ``CodeHostBackend``
        Protocol surface. Returns ``[]`` (no separate required-context gate).
        """
        del slug, pr_id
        return []

    def fetch_pr_changed_paths(self, *, slug: str, pr_id: int) -> list[str]:
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}/diffs"])
        if rc != 0 or not out.strip():
            return []
        try:
            diffs = json.loads(out)
        except json.JSONDecodeError:
            return []
        if not isinstance(diffs, list):
            return []
        paths: list[str] = []
        for entry in diffs:
            if not isinstance(entry, dict):
                continue
            new_path = str(entry.get("new_path") or entry.get("old_path") or "").strip()
            if new_path:
                paths.append(new_path)
        return paths

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
