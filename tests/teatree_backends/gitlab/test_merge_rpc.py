"""GitLab §17.4.3 merge RPC over httpx — fail-closed reads + byte-for-byte merge parity (#4007).

The whole surface used to shell out to the ``glab`` BINARY, which the deploy image
never installs, so every call raised ``FileNotFoundError`` there. These tests pin
the httpx port: every read still degrades to its fail-closed sentinel on any forge
failure, and ``merge_pr_squash_bound`` still renders a failure into the
``(returncode, stdout, stderr)`` triple that ``core.merge.merge_response``
classifies — asserted by running the REAL classifier, not by matching strings.
"""

import json
from pathlib import Path

import httpx
import pytest

from teatree.backends.gitlab import merge_rpc
from teatree.backends.gitlab.merge_rpc import GitLabApiMergeRpc
from teatree.core.backend_protocols import (
    CHANGED_PATHS_UNAVAILABLE,
    BackendResolutionError,
    MergeConflictState,
    changed_paths_unavailable,
    rollup_query_failed,
)
from teatree.core.merge.errors import MergeHeadMovedError, MergePreconditionError, MergeTransientError
from teatree.core.merge.merge_response import _raise_bound_merge_failure

_SLUG = "acme/widget"
_ENCODED = "acme%2Fwidget"
_IID = 6264
_SHA = "a" * 40


class _FakeClient:
    """A ``GitLabAPI`` stand-in: scripted payloads per endpoint, recorded calls."""

    def __init__(
        self,
        *,
        get: object = None,
        paginated: list[dict[str, object]] | None = None,
        put: httpx.Response | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._get = get
        self._paginated = paginated or []
        self._put = put
        self._raises = raises
        self.get_calls: list[str] = []
        self.paginated_calls: list[str] = []
        self.put_calls: list[tuple[str, dict[str, object], bool]] = []

    def get_json(self, endpoint: str) -> object:
        self.get_calls.append(endpoint)
        if self._raises is not None:
            raise self._raises
        return self._get

    def get_json_paginated(self, endpoint: str) -> list[dict[str, object]]:
        self.paginated_calls.append(endpoint)
        if self._raises is not None:
            raise self._raises
        return self._paginated

    def put_response(
        self,
        endpoint: str,
        payload: dict[str, object] | None = None,
        *,
        idempotent: bool = True,
    ) -> httpx.Response:
        self.put_calls.append((endpoint, payload or {}, idempotent))
        if self._raises is not None:
            raise self._raises
        assert self._put is not None
        return self._put


def _response(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("PUT", "https://gitlab.example/api"))


def _rpc(**kwargs: object) -> GitLabApiMergeRpc:
    return GitLabApiMergeRpc(_FakeClient(**kwargs))


# Every way the transport can fail to answer: HTTP status, transport-level, missing
# credential, unparsable body. Each must degrade to the method's fail-closed sentinel.
_READ_FAILURES = [
    httpx.HTTPStatusError("404", request=httpx.Request("GET", "https://x"), response=httpx.Response(404)),
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    BackendResolutionError("no token"),
    json.JSONDecodeError("bad", "{", 0),
]


class TestNoBinaryDependency:
    @pytest.mark.parametrize("primitive", ["shutil", "subprocess", "utils.run", "run_allowed_to_fail"])
    def test_module_shells_out_to_nothing(self, primitive: str) -> None:
        # The whole point of #4007: the deploy image ships no `glab`, so any
        # subprocess primitive here would re-strand the merge surface.
        assert primitive not in Path(merge_rpc.__file__).read_text(encoding="utf-8")


class TestFetchLiveHeadSha:
    def test_reads_the_encoded_project_mr_endpoint(self) -> None:
        client = _FakeClient(get={"sha": _SHA})
        assert GitLabApiMergeRpc(client).fetch_live_head_sha(slug=_SLUG, pr_id=_IID) == _SHA
        assert client.get_calls == [f"projects/{_ENCODED}/merge_requests/{_IID}"]

    @pytest.mark.parametrize("failure", _READ_FAILURES)
    def test_empty_on_any_forge_failure(self, failure: Exception) -> None:
        assert _rpc(raises=failure).fetch_live_head_sha(slug=_SLUG, pr_id=_IID) == ""

    def test_empty_on_non_object_payload(self) -> None:
        assert _rpc(get=[1, 2]).fetch_live_head_sha(slug=_SLUG, pr_id=_IID) == ""


class TestFetchPrMergeState:
    def test_merged_state_is_upper_cased_for_github_parity(self) -> None:
        state = _rpc(get={"state": "merged", "merge_commit_sha": "deadbeef"}).fetch_pr_merge_state(
            slug=_SLUG, pr_id=_IID
        )
        assert (state.state, state.merge_commit_oid) == ("MERGED", "deadbeef")
        assert state.is_merged

    def test_squash_commit_sha_is_the_fallback_oid(self) -> None:
        state = _rpc(get={"state": "merged", "squash_commit_sha": "squashed"}).fetch_pr_merge_state(
            slug=_SLUG, pr_id=_IID
        )
        assert state.merge_commit_oid == "squashed"

    @pytest.mark.parametrize("failure", _READ_FAILURES)
    def test_empty_state_on_any_forge_failure(self, failure: Exception) -> None:
        state = _rpc(raises=failure).fetch_pr_merge_state(slug=_SLUG, pr_id=_IID)
        assert (state.state, state.merge_commit_oid) == ("", "")
        assert not state.is_merged


class TestFetchPrMergeStateConflictAxis:
    """The conflict axis must be READ, not left at its ``UNKNOWN`` default (#4193).

    The mapper existed in ``forge_merge_rpc`` but the #4007 httpx port never called it,
    so every GitLab MR reported ``UNKNOWN``. ``mr_conflict`` turns any non-``CLEAN``
    verdict into a signal, which manufactured one permanent "merge state unreadable"
    signal per open MR — indistinguishable from the genuine "the forge is still
    computing it" case the tri-state exists to report.
    """

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"has_conflicts": False, "merge_status": "can_be_merged"}, MergeConflictState.CLEAN),
            ({"has_conflicts": True, "merge_status": "cannot_be_merged"}, MergeConflictState.CONFLICTED),
            # `has_conflicts` is authoritative on its own — a draft / unapproved MR still
            # reports its real conflict state instead of hiding behind `merge_status`.
            ({"has_conflicts": True, "merge_status": "checking"}, MergeConflictState.CONFLICTED),
            ({"merge_status": "cannot_be_merged"}, MergeConflictState.CONFLICTED),
            # Still computing: `has_conflicts` is a default here, not a finding.
            ({"has_conflicts": False, "merge_status": "checking"}, MergeConflictState.UNKNOWN),
            ({"has_conflicts": False, "merge_status": "unchecked"}, MergeConflictState.UNKNOWN),
            ({}, MergeConflictState.UNKNOWN),
        ],
    )
    def test_maps_the_gitlab_pair_onto_the_conflict_axis(
        self, payload: dict[str, object], expected: MergeConflictState
    ) -> None:
        state = _rpc(get={"state": "opened", **payload}).fetch_pr_merge_state(slug=_SLUG, pr_id=_IID)
        assert state.conflict is expected

    def test_a_mergeable_mr_is_clean_so_the_scanner_stays_silent(self) -> None:
        """The regression itself: a healthy open MR must produce NO conflict signal."""
        rpc = _rpc(get={"state": "opened", "has_conflicts": False, "merge_status": "can_be_merged"})
        assert rpc.fetch_pr_merge_state(slug=_SLUG, pr_id=_IID).conflict is MergeConflictState.CLEAN

    def test_an_unreadable_mr_stays_unknown(self) -> None:
        state = _rpc(raises=httpx.ConnectError("refused")).fetch_pr_merge_state(slug=_SLUG, pr_id=_IID)
        assert state.conflict is MergeConflictState.UNKNOWN


class TestFetchPrAuthor:
    def test_username(self) -> None:
        assert _rpc(get={"author": {"username": "souliane"}}).fetch_pr_author(slug=_SLUG, pr_id=_IID) == "souliane"

    def test_empty_when_author_is_not_an_object(self) -> None:
        assert _rpc(get={"author": "souliane"}).fetch_pr_author(slug=_SLUG, pr_id=_IID) == ""

    @pytest.mark.parametrize("failure", _READ_FAILURES)
    def test_empty_on_any_forge_failure(self, failure: Exception) -> None:
        assert _rpc(raises=failure).fetch_pr_author(slug=_SLUG, pr_id=_IID) == ""


class TestFetchPrSameRepo:
    def test_same_project_ids_are_same_repo(self) -> None:
        assert _rpc(get={"source_project_id": 7, "target_project_id": 7}).fetch_pr_same_repo(slug=_SLUG, pr_id=_IID)

    def test_distinct_project_ids_are_a_fork(self) -> None:
        assert (
            _rpc(get={"source_project_id": 9, "target_project_id": 7}).fetch_pr_same_repo(slug=_SLUG, pr_id=_IID)
            is False
        )

    def test_indeterminate_when_ids_are_not_integers(self) -> None:
        assert _rpc(get={"iid": _IID}).fetch_pr_same_repo(slug=_SLUG, pr_id=_IID) is None

    @pytest.mark.parametrize("failure", _READ_FAILURES)
    def test_indeterminate_on_any_forge_failure(self, failure: Exception) -> None:
        assert _rpc(raises=failure).fetch_pr_same_repo(slug=_SLUG, pr_id=_IID) is None


class TestFetchRequiredChecks:
    def test_reads_the_mr_pipelines_endpoint(self) -> None:
        client = _FakeClient(get=[{"id": 1, "status": "success"}])
        rollup = GitLabApiMergeRpc(client).fetch_required_checks_rollup(slug=_SLUG, pr_id=_IID)
        assert rollup == [{"id": 1, "status": "success"}]
        assert client.get_calls == [f"projects/{_ENCODED}/merge_requests/{_IID}/pipelines"]

    def test_non_object_entries_are_dropped(self) -> None:
        assert _rpc(get=[{"id": 1}, "junk"]).fetch_required_checks_rollup(slug=_SLUG, pr_id=_IID) == [{"id": 1}]

    @pytest.mark.parametrize("failure", _READ_FAILURES)
    def test_sentinel_on_any_forge_failure(self, failure: Exception) -> None:
        assert rollup_query_failed(_rpc(raises=failure).fetch_required_checks_rollup(slug=_SLUG, pr_id=_IID))

    def test_sentinel_on_non_list_payload(self) -> None:
        assert rollup_query_failed(_rpc(get={"status": "success"}).fetch_required_checks_rollup(slug=_SLUG, pr_id=_IID))

    def test_no_separate_required_context_gate_on_gitlab(self) -> None:
        assert _rpc().fetch_required_status_check_contexts(slug=_SLUG, pr_id=_IID) == []


class TestFetchPrChangedPaths:
    def test_new_path_wins_and_old_path_is_the_fallback(self) -> None:
        client = _FakeClient(paginated=[{"new_path": "a.py", "old_path": "z.py"}, {"old_path": "b.py"}])
        paths = GitLabApiMergeRpc(client).fetch_pr_changed_paths(slug=_SLUG, pr_id=_IID)
        assert paths == ["a.py", "b.py"]
        assert client.paginated_calls == [f"projects/{_ENCODED}/merge_requests/{_IID}/diffs?per_page=100"]

    def test_entries_carrying_neither_path_are_skipped(self) -> None:
        # `glab --jq '.new_path // .old_path'` emitted the literal string "null" for
        # such an entry; a phantom path must never reach the substrate detector.
        paths = _rpc(paginated=[{"new_path": None, "old_path": None}, {"new_path": "a.py"}]).fetch_pr_changed_paths(
            slug=_SLUG, pr_id=_IID
        )
        assert paths == ["a.py"]

    @pytest.mark.parametrize("failure", _READ_FAILURES)
    def test_sentinel_on_any_forge_failure(self, failure: Exception) -> None:
        paths = _rpc(raises=failure).fetch_pr_changed_paths(slug=_SLUG, pr_id=_IID)
        assert changed_paths_unavailable(paths)
        assert paths == [CHANGED_PATHS_UNAVAILABLE]


def _classify(result: object) -> None:
    """Run the REAL core classifier over a merge result — the parity contract itself."""
    _raise_bound_merge_failure(
        result=result,
        slug=_SLUG,
        pr_id=_IID,
        expected_head_oid=_SHA,
        host_kind="gitlab",
    )


class TestMergePrSquashBound:
    def test_success_binds_the_sha_and_squashes_non_idempotently(self) -> None:
        client = _FakeClient(put=_response(200, json.dumps({"merge_commit_sha": "landed"})))
        result = GitLabApiMergeRpc(client).merge_pr_squash_bound(slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA)
        assert (result.returncode, result.merged_sha) == (0, "landed")
        endpoint, payload, idempotent = client.put_calls[0]
        assert endpoint == f"projects/{_ENCODED}/merge_requests/{_IID}/merge"
        assert payload == {"sha": _SHA, "squash": True}
        # A blind transport replay of a merge that already LANDED would 405 and brick
        # the keystone; core reconciles then retries, so the transport must not.
        assert idempotent is False

    def test_sha_is_the_fallback_merged_oid(self) -> None:
        result = _rpc(put=_response(200, json.dumps({"sha": "landed"}))).merge_pr_squash_bound(
            slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA
        )
        assert result.merged_sha == "landed"

    def test_unparseable_success_body_leaves_the_merged_sha_empty(self) -> None:
        result = _rpc(put=_response(200, "{not json")).merge_pr_squash_bound(
            slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA
        )
        assert (result.returncode, result.merged_sha) == (0, "")

    def test_sha_mismatch_409_classifies_head_moved(self) -> None:
        result = _rpc(
            put=_response(409, json.dumps({"message": "SHA does not match HEAD of source branch"}))
        ).merge_pr_squash_bound(slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA)
        assert result.returncode != 0
        with pytest.raises(MergeHeadMovedError, match="head moved off"):
            _classify(result)

    @pytest.mark.parametrize("status", [405, 422])
    def test_policy_refusal_is_never_retried(self, status: int) -> None:
        result = _rpc(put=_response(status, json.dumps({"message": "Method Not Allowed"}))).merge_pr_squash_bound(
            slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA
        )
        with pytest.raises(MergePreconditionError):
            _classify(result)

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_failures_classify_transient(self, status: int) -> None:
        result = _rpc(put=_response(status, "")).merge_pr_squash_bound(slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA)
        with pytest.raises(MergeTransientError):
            _classify(result)

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.RemoteProtocolError("server disconnected"),
            httpx.ProxyError("bad proxy"),
        ],
    )
    def test_every_transport_failure_classifies_transient(self, failure: Exception) -> None:
        # The forge issued NO verdict, so the merge is retryable — core re-probes
        # `_already_merged_at` before each retry, which is what makes this safe.
        result = _rpc(raises=failure).merge_pr_squash_bound(slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA)
        assert result.returncode != 0
        with pytest.raises(MergeTransientError):
            _classify(result)

    def test_missing_credential_is_a_refusal_not_a_retry(self) -> None:
        # A credential outage is not momentary — retrying it three times only
        # delays the loud failure the operator has to fix.
        result = _rpc(raises=BackendResolutionError("no token")).merge_pr_squash_bound(
            slug=_SLUG, pr_id=_IID, expected_head_oid=_SHA
        )
        with pytest.raises(MergePreconditionError):
            _classify(result)
