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
from typing import cast

from teatree.core.backend_protocols import (
    CHANGED_PATHS_UNAVAILABLE,
    ROLLUP_QUERY_FAILED,
    ForgeMergeResult,
    PrMergeState,
)
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


def _github_rules_required_contexts(rc: int, out: str, err: str) -> set[str] | None:
    """Required contexts from the effective-rules endpoint, or ``None`` when unreadable.

    ``repos/<slug>/rules/branches/<base>`` is readable with a fine-grained PAT's
    ``contents``/``metadata`` read and returns the effective required checks from
    BOTH classic branch protection AND rulesets — the PAT-readable source the
    §17.4.3 step-3 required set should prefer. Unions
    ``parameters.required_status_checks[].context`` across every
    ``required_status_checks`` rule.

    A determinate ``set()`` (possibly empty) is returned for any readable,
    parseable list — an empty set means the branch has no ``required_status_checks``
    rule (no gate). ``None`` signals an INDETERMINATE read (non-zero rc, unparsable
    body, or a non-list payload) so the caller can fall back to the legacy
    protection endpoint before deciding whether to fail closed.
    """
    del err  # the caller distinguishes readable/unreadable via rc + body parseability
    if rc != 0:
        return None
    try:
        data = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    contexts: set[str] = set()
    for rule in data:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters")
        if not isinstance(params, dict):
            continue
        for check in params.get("required_status_checks") or []:
            if isinstance(check, dict) and isinstance(check.get("context"), str) and check["context"]:
                contexts.add(check["context"])
    return contexts


def _github_protection_required_contexts(rc: int, out: str, err: str) -> set[str] | None:
    """Required contexts from the legacy branch-protection endpoint, or ``None`` if unreadable.

    ``None`` signals an INDETERMINATE read — a fine-grained PAT WITHOUT the
    "Administration" permission gets HTTP 403 ("Resource not accessible by personal
    access token" / "must have admin") on this endpoint, and a 5xx / network /
    unparsable body is equally unreadable. On ``None`` the caller falls back to the
    rules-endpoint result rather than failing closed on this source alone.

    A determinate ``set()`` is returned for a genuine "no classic protection" 404
    (``Branch not protected`` / ``Required status checks not enabled`` / ``Not
    Found``); otherwise the UNION of the legacy ``contexts`` array and the newer
    ``checks[].context`` array (GitHub returns both; either may carry a name).
    """
    if rc != 0:
        body = f"{out}\n{err}".lower()
        # ``base`` is already confirmed a real branch, so a 404 here can only mean
        # "no protection configured" — never "no such branch". A 403 (no Admin
        # permission), 5xx, or network error is NOT determinate → ``None``.
        if "branch not protected" in body or "required status checks not enabled" in body or "not found" in body:
            return set()
        return None
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    contexts: set[str] = set()
    for ctx in data.get("contexts") or []:
        if isinstance(ctx, str) and ctx:
            contexts.add(ctx)
    for check in data.get("checks") or []:
        if isinstance(check, dict) and isinstance(check.get("context"), str) and check["context"]:
            contexts.add(check["context"])
    return contexts


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

    def fetch_pr_same_repo(self, *, slug: str, pr_id: int) -> bool | None:
        """Tri-state head-branch provenance — the §17.4.3 fork gate input (#3244).

        ``isCrossRepository`` True ⇒ a fork head (returns ``False`` — NOT same repo);
        False ⇒ a same-repo head (returns ``True``). Any forge error or a
        non-boolean payload returns ``None`` so the provenance gate fails closed to
        the identity+visibility author check rather than trusting an unknown head.
        """
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "isCrossRepository", "--jq", ".isCrossRepository"],
        )
        if rc != 0:
            return None
        answer = out.strip().lower()
        if answer == "true":
            return False
        if answer == "false":
            return True
        return None

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
        """Required-status-check contexts for the PR's base branch — §17.4.3 step 3.

        The AUTHORITATIVE required set: only a context the repo requires as a status
        check may block the merge. A check NOT in this set (``eval``, advisory lanes,
        …) never blocks regardless of its conclusion.

        Resolved from TWO sources and unioned so a fine-grained PAT still gets a
        determinate answer. The preferred source is the effective-rules endpoint
        ``repos/<slug>/rules/branches/<base>``, readable with a fine-grained PAT's
        ``contents``/``metadata`` read, which returns the effective required checks
        from BOTH classic branch protection AND rulesets. The legacy endpoint
        ``branches/<base>/protection/required_status_checks`` is the union fallback,
        but a fine-grained PAT WITHOUT "Administration" gets HTTP 403 on it; a 403
        here is treated as INDETERMINATE for this source, NOT a hard failure — the
        rules-endpoint result stands.

        Returns one ``{"context": <name>}`` entry per required context (the union of
        the two readable sources), or ``[]`` when at least one source is readable and
        determinately reports NO required gate (no gate → green, mergeable).

        Returns ``[ROLLUP_QUERY_FAILED]`` (fail CLOSED) STRICTLY when the required
        set is genuinely indeterminate: the base branch cannot be read, or NEITHER
        endpoint could be read (both error non-deterministically / unparsable). A
        real inability to determine the required set must still refuse the merge.
        """
        rc, out, _ = self._run(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "baseRefName", "--jq", ".baseRefName"],
        )
        base = out.strip()
        if rc != 0 or not base:
            return [ROLLUP_QUERY_FAILED]
        rules_rc, rules_out, rules_err = self._run(["api", f"repos/{slug}/rules/branches/{base}"])
        rules_contexts = _github_rules_required_contexts(rules_rc, rules_out, rules_err)
        prot_rc, prot_out, prot_err = self._run(
            ["api", f"repos/{slug}/branches/{base}/protection/required_status_checks"],
        )
        protection_contexts = _github_protection_required_contexts(prot_rc, prot_out, prot_err)
        determinate = [contexts for contexts in (rules_contexts, protection_contexts) if contexts is not None]
        if not determinate:
            # Neither the rules endpoint nor the legacy protection endpoint could be
            # read — the required set is genuinely indeterminate → fail CLOSED.
            return [ROLLUP_QUERY_FAILED]
        union: set[str] = set()
        for contexts in determinate:
            union |= contexts
        return [{"context": ctx} for ctx in sorted(union)]

    def fetch_pr_changed_paths(self, *, slug: str, pr_id: int) -> list[str]:
        """Every changed path on the PR — PAGINATED to completion (§17.4.3, substrate detector).

        ``gh pr view --json files`` caps the file list at 100 with NO pagination, so
        a >100-file PR silently truncated its diff and a substrate change past the cap
        went undetected. The ``repos/<slug>/pulls/<n>/files`` REST endpoint with
        ``--paginate`` follows every page, so the returned list is complete. A non-zero
        rc means the diff could not be read to completion → return the
        ``CHANGED_PATHS_UNAVAILABLE`` sentinel so the caller fails CLOSED (holds the
        merge), never a silently-empty/partial list.
        """
        rc, out, _ = self._run(
            ["api", "--paginate", f"repos/{slug}/pulls/{pr_id}/files", "--jq", ".[].filename"],
        )
        if rc != 0:
            return [CHANGED_PATHS_UNAVAILABLE]
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

    def _fetch_mr(self, *, slug: str, pr_id: int) -> RawAPIDict | None:
        """Fetch and JSON-parse the ``merge_requests/{id}`` object; ``None`` on any error.

        The head-SHA, merge-state, draft-flag, and author reads all pull the
        same MR object; this centralises the ``glab api`` call plus the
        ``json.loads`` / dict-shape guard so each reader is just its field
        extraction. ``None`` covers a non-zero rc, an empty body, a JSON parse
        failure, or a non-object payload.
        """
        rc, out, _ = self._run(["api", f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}"])
        if rc != 0 or not out.strip():
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        mr = self._fetch_mr(slug=slug, pr_id=pr_id)
        return str(mr.get("sha") or "") if mr is not None else ""

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        mr = self._fetch_mr(slug=slug, pr_id=pr_id)
        if mr is None:
            return PrMergeState(state="", merge_commit_oid="")
        state = str(mr.get("state") or "").upper()  # "merged" → "MERGED" (parity with GitHub)
        oid = str(mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or "")
        return PrMergeState(state=state, merge_commit_oid=oid)

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        mr = self._fetch_mr(slug=slug, pr_id=pr_id)
        if mr is None:
            return False
        # ``draft`` is canonical on modern GitLab; ``work_in_progress`` is the legacy
        # field kept for compatibility — accept either.
        return bool(mr.get("draft") or mr.get("work_in_progress"))

    def fetch_pr_author(self, *, slug: str, pr_id: int) -> str:
        """The MR author ``username`` — the §17.4.3 author-gate input (#1773).

        Returns ``""`` on any error; the empty author is fail-closed at the
        keystone (an author that cannot be proved trusted does not auto-merge
        on a public repo).
        """
        mr = self._fetch_mr(slug=slug, pr_id=pr_id)
        if mr is None:
            return ""
        author = mr.get("author")
        if not isinstance(author, dict):
            return ""
        return str(cast("RawAPIDict", author).get("username") or "")

    def fetch_pr_same_repo(self, *, slug: str, pr_id: int) -> bool | None:
        """Tri-state head-branch provenance — the §17.4.3 fork gate input (#3244).

        A same-repo MR has ``source_project_id == target_project_id``; a fork MR
        crosses projects. Any forge error or a non-integer project id returns
        ``None`` so the provenance gate fails closed to the identity+visibility
        author check. This is what makes GitLab overlay MRs cross the same gate.
        """
        mr = self._fetch_mr(slug=slug, pr_id=pr_id)
        if mr is None:
            return None
        source = mr.get("source_project_id")
        target = mr.get("target_project_id")
        if not isinstance(source, int) or not isinstance(target, int):
            return None
        return source == target

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
        """Every changed path on the MR — PAGINATED to completion (§17.4.3, substrate detector).

        The ``merge_requests/<iid>/diffs`` endpoint is paginated; a single un-paginated
        call truncated a large MR's diff and a substrate change past the first page went
        undetected. ``--paginate`` with a per-entry ``--jq`` follows every page and emits
        one path per line (``new_path`` falling back to ``old_path``). A non-zero rc means
        the diff could not be read to completion → return the ``CHANGED_PATHS_UNAVAILABLE``
        sentinel so the caller fails CLOSED (holds the merge), never a partial list.
        """
        rc, out, _ = self._run(
            [
                "api",
                "--paginate",
                f"projects/{glab_project_path(slug)}/merge_requests/{pr_id}/diffs?per_page=100",
                "--jq",
                ".[] | (.new_path // .old_path)",
            ],
        )
        if rc != 0:
            return [CHANGED_PATHS_UNAVAILABLE]
        return [line.strip() for line in out.splitlines() if line.strip()]

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
