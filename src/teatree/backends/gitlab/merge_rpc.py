"""GitLab §17.4.3 merge RPC over the httpx client — no binary, no second transport (#4007).

The nine merge-RPC methods :class:`~teatree.backends.gitlab.GitLabCodeHost` exposes
used to run the ``glab`` BINARY. The deploy image installs ``gh`` only, so on a
GitLab-hosted overlay every one of them raised ``FileNotFoundError`` in the
container: the keystone merge and every scanner built on ``fetch_pr_merge_state``
failed closed, silently. They now speak the same REST endpoints over the
:class:`~teatree.backends.gitlab.api.GitLabAPI` transport every other GitLab read
already uses — one transport for the whole forge, and one credential
(``GITLAB_TOKEN``) instead of an ambient binary login.

Raw I/O only. Every transient / head-moved / policy-refusal verdict stays in
:mod:`teatree.core.merge.merge_response`, which classifies the
``(returncode, stdout, stderr)`` triple — so :meth:`GitLabApiMergeRpc.merge_pr_squash_bound`
renders an HTTP outcome back into that triple (status code AND response body) and
the byte-for-byte error parity the keystone tests pin is unchanged.
"""

import json
import logging
from typing import cast

import httpx

from teatree.backends.gitlab.api import GitLabAPI
from teatree.core.backend_protocols import (
    CHANGED_PATHS_UNAVAILABLE,
    ROLLUP_QUERY_FAILED,
    BackendResolutionError,
    ForgeMergeResult,
    PrMergeState,
)
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

# Every way the transport can fail to hand back a parsed body: a non-2xx status
# (``get_json`` raise_for_status-es), a transport-level failure, an unresolved
# credential, or an unparsable payload (``json.JSONDecodeError`` is a ``ValueError``).
# The reads below catch this union and degrade to their fail-closed sentinel — the
# same verdict the pre-port non-zero ``glab`` exit produced.
_READ_FAILURES = (httpx.HTTPError, BackendResolutionError, ValueError)

# ``core.merge.merge_response`` classifies a merge failure from the lower-cased
# stdout+stderr. A transport-level failure means the forge issued NO verdict, so it
# must classify TRANSIENT (core re-probes whether the merge actually landed before
# each retry) — this prefix is that classifier's own vocabulary for it, carried
# alongside the real exception so the log still names the concrete failure.
_TRANSPORT_FAILURE_PREFIX = "temporary failure reaching the GitLab API"


def _mr_endpoint(slug: str, pr_id: int) -> str:
    """The MR's REST path, with the project identifier URL-encoded.

    GitLab's REST API takes the project identifier ``group/repo`` (or
    ``group/subgroup/repo``) URL-encoded — the slashes become ``%2F``. One
    function builds it for every endpoint below, so no read can address a
    differently-spelled project.
    """
    return f"projects/{slug.replace('/', '%2F')}/merge_requests/{pr_id}"


class GitLabApiMergeRpc:
    """The §17.4.3 GitLab merge surface — MR reads plus the SHA-bound squash merge."""

    def __init__(self, client: GitLabAPI) -> None:
        self._client = client

    def _fetch_mr(self, *, slug: str, pr_id: int) -> RawAPIDict | None:
        """The ``merge_requests/{iid}`` object, or ``None`` when it could not be read.

        The head-SHA, merge-state, draft-flag, author and provenance reads all pull
        the same MR object; centralising the request plus the failure/shape guard
        leaves each reader as just its field extraction.
        """
        data = self._read(_mr_endpoint(slug, pr_id))
        return cast("RawAPIDict", data) if isinstance(data, dict) else None

    def _read(self, endpoint: str) -> object:
        """A GET whose every failure mode degrades to ``None``, logged not swallowed."""
        try:
            return self._client.get_json(endpoint)
        except _READ_FAILURES as exc:
            logger.warning("GitLab merge-RPC read of %s failed (%s) — failing closed", endpoint, exc)
            return None

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
        pipelines = self._read(f"{_mr_endpoint(slug, pr_id)}/pipelines")
        if not isinstance(pipelines, list):
            return [ROLLUP_QUERY_FAILED]
        entries = cast("list[object]", pipelines)
        return [cast("RawAPIDict", entry) for entry in entries if isinstance(entry, dict)]

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
        """Every changed path on the MR — PAGINATED (§17.4.3, substrate detector).

        The ``merge_requests/<iid>/diffs`` endpoint is paginated; a single
        un-paginated call truncated a large MR's diff and a substrate change past
        the first page went undetected. Any read failure returns the
        ``CHANGED_PATHS_UNAVAILABLE`` sentinel so the caller fails CLOSED (holds
        the merge) rather than judging a partial list. The transport walks at most
        100 pages of 100, and warns if it ever reaches that bound — several times
        GitLab's own cap on the files it will report for one MR, so the walk ends
        on an empty page, not on the bound.
        """
        endpoint = f"{_mr_endpoint(slug, pr_id)}/diffs?per_page=100"
        try:
            diffs = self._client.get_json_paginated(endpoint)
        except _READ_FAILURES as exc:
            logger.warning("GitLab MR diff read of %s failed (%s) — failing closed", endpoint, exc)
            return [CHANGED_PATHS_UNAVAILABLE]
        paths = [entry.get("new_path") or entry.get("old_path") for entry in diffs]
        return [path.strip() for path in paths if isinstance(path, str) and path.strip()]

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> ForgeMergeResult:
        """``PUT merge_requests/<iid>/merge`` bound to *expected_head_oid*, squashed.

        Issued NON-idempotently: a merge that reached GitLab and only lost its
        response must not be blindly replayed by the retry transport (the replay
        would 405 and brick the keystone). Core owns the merge retry —
        :func:`core.merge.execution.execute_bound_merge` re-probes whether the
        merge actually landed before each one.
        """
        endpoint = f"{_mr_endpoint(slug, pr_id)}/merge"
        try:
            response = self._client.put_response(
                endpoint,
                {"sha": expected_head_oid, "squash": True},
                idempotent=False,
            )
        except httpx.RequestError as exc:
            return _transport_failure(exc)
        except BackendResolutionError as exc:
            return ForgeMergeResult(returncode=1, stdout="", stderr=str(exc), merged_sha="")
        return _merge_result(response)


def _transport_failure(exc: httpx.RequestError) -> ForgeMergeResult:
    """A merge whose request never got an answer — rendered so core retries it."""
    logger.warning("GitLab bound merge failed at the transport (%s: %s)", type(exc).__name__, exc)
    stderr = f"{_TRANSPORT_FAILURE_PREFIX}: {type(exc).__name__}: {exc}"
    return ForgeMergeResult(returncode=1, stdout="", stderr=stderr, merged_sha="")


def _merge_result(response: httpx.Response) -> ForgeMergeResult:
    """Render the merge response into the triple ``core.merge.merge_response`` reads.

    The status code AND the body go into stderr because both carry classification
    signal: GitLab answers a moved head with ``409`` plus ``SHA does not match HEAD
    of source branch`` (head-moved), a re-merge with ``405`` and an unmergeable MR
    with ``422`` (policy refusals, never retried), and an outage with ``502``/``503``/
    ``504`` (transient) — the exact markers the classifier matches on.
    """
    body = response.text
    if not response.is_success:
        logger.warning("GitLab bound merge refused with HTTP %s: %s", response.status_code, body.strip())
        return ForgeMergeResult(
            returncode=1,
            stdout=body,
            stderr=f"HTTP {response.status_code}: {body.strip()}",
            merged_sha="",
        )
    return ForgeMergeResult(returncode=0, stdout=body, stderr="", merged_sha=_merged_sha(body))


def _merged_sha(body: str) -> str:
    """The landed commit from a successful merge body; ``""`` when unreadable.

    An empty ``merged_sha`` is not a failure — the caller falls back to the bound
    head OID, so a truncated success body still records the right landing.
    """
    try:
        merged = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        return ""
    if not isinstance(merged, dict):
        return ""
    payload = cast("RawAPIDict", merged)
    return str(payload.get("merge_commit_sha") or payload.get("sha") or "")
