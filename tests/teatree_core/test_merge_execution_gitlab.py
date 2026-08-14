"""GitLab transport for the §17.4 keystone merge (sibling of test_merge_execution.py).

The §17.4.3 live-forge reads (:meth:`CodeHostQuery.live_head_sha`,
:meth:`CodeHostQuery.pr_draft_state`, :meth:`CodeHostQuery.required_checks_status`)
and ``execute_bound_merge`` originally hardcoded ``gh pr view`` / ``gh api``,
which left GitLab MRs unreachable through the sanctioned path. These
tests assert that each read dispatches by code-host kind (carried on the
:class:`PrRef`'s ``host_kind``) and invokes the equivalent GitLab REST call.

Only the unstoppable external — the forge HTTP call — is stubbed; every teatree
model / FSM / DB write is real. Since #4007 the GitLab side is the httpx
``GitLabAPI`` client, not the ``glab`` binary the deploy image never installs.
"""

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase

from teatree.core.backend_protocols import DraftState
from teatree.core.merge import (
    CodeHostQuery,
    MergeHeadMovedError,
    MergeOutcome,
    MergePreconditionError,
    execute_bound_merge,
    merge_ticket_pr,
    resolve_host_kind,
)
from teatree.core.merge.execution import assert_not_draft
from teatree.core.models import MergeAudit, MergeClear, Ticket
from teatree.utils.pr_ref import PrRef
from tests.teatree_core.conftest import seed_merge_safe_verdict

_DRAFT_PROBE = "teatree.backends.gitlab.client.GitLabCodeHost.fetch_pr_draft_state"


@contextmanager
def _http_draft_state(state: DraftState) -> Iterator[None]:
    """Script the HTTP-transport draft probe (the one read that is not ``glab``)."""
    with patch(_DRAFT_PROBE, return_value=state):
        yield


@pytest.fixture(autouse=True)
def _draft_probe_answers_non_draft() -> Iterator[None]:
    """Default the HTTP draft probe to a CONFIRMED non-draft MR.

    The step-4 gate now holds on an unreadable draft flag, and these cases stub
    only ``glab`` — without a scripted HTTP answer every keystone flow here would
    refuse on the draft probe rather than on the behaviour under test.
    """
    with _http_draft_state(DraftState.NOT_DRAFT):
        yield


def _gitlab_query() -> CodeHostQuery:
    """A ``CodeHostQuery`` bound to the GitLab test MR (the transport under test)."""
    return CodeHostQuery.for_ref(PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"))


# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these GitLab transport tests pre-date it, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


_SHA = "a" * 40
_GITLAB_ISSUE_URL = "https://gitlab.com/acme/widget/-/issues/6264"
_GITLAB_SELF_HOSTED_URL = "https://gitlab.example.com/acme/widget/-/issues/6264"
_GITLAB_SLUG = "acme/widget"
_PR_IID = 6264


def _clear(ticket: Ticket, **overrides: object) -> MergeClear:
    defaults: dict[str, object] = {
        "ticket": ticket,
        "pr_id": _PR_IID,
        "slug": _GITLAB_SLUG,
        "reviewed_sha": _SHA,
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": MergeClear.VerifyResult.GREEN,
        "blast_class": MergeClear.BlastClass.DOCS,
    }
    defaults.update(overrides)
    return MergeClear.objects.create(**defaults)


def _response(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("PUT", "https://gitlab.example/api"))


_PROJECT_ID = 42


class _GitLabApiStub:
    """Scripted GitLab REST payloads keyed by endpoint; records every endpoint hit."""

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def __init__(  # noqa: PLR0913 — test stub mirrors the response surface; each field models one wire-API field.
        self,
        *,
        sha: str = _SHA,
        draft: bool = False,
        state: str = "opened",
        pipeline_status: str = "success",
        merge_status: int = 200,
        merge_body: str = "",
        merge_sha: str = "merged0deadbeef",
    ) -> None:
        self.sha = sha
        self.draft = draft
        self.state = state
        self.pipeline_status = pipeline_status
        self.merge_status = merge_status
        self.merge_body = merge_body
        self.merge_sha = merge_sha
        self.calls: list[str] = []
        self.merge_payloads: list[dict[str, object]] = []

    def get_json(self, endpoint: str) -> object:
        self.calls.append(endpoint)
        if "/pipelines" in endpoint:
            return [{"id": 12345, "status": self.pipeline_status, "sha": self.sha}]
        if "/merge_requests/" in endpoint:
            return {"iid": _PR_IID, "sha": self.sha, "draft": self.draft, "state": self.state}
        return {}

    def resolve_project(self, repo: str) -> object:
        """The project the draft probe resolves before it can read the MR.

        Without it the probe names no project, answers UNKNOWN, and the keystone refuses
        to merge — correct for an unread probe, and it would fail every merge test here
        for a reason none of them is about.
        """
        self.calls.append(f"resolve_project:{repo}")
        return SimpleNamespace(project_id=_PROJECT_ID, full_path=repo)

    def resolve_project_from_remote(self, repo: str) -> object:
        return self.resolve_project(repo)

    def get_json_paginated(self, endpoint: str) -> list[dict[str, object]]:
        self.calls.append(endpoint)
        # A real open MR always changes >=1 file, so an empty diff is a failed read
        # the substrate gate holds on.
        return [{"new_path": "README.md"}] if "/diffs" in endpoint else []

    def put_response(
        self,
        endpoint: str,
        payload: dict[str, object] | None = None,
        *,
        idempotent: bool = True,
    ) -> httpx.Response:
        self.calls.append(endpoint)
        self.merge_payloads.append(dict(payload or {}))
        if self.merge_status != httpx.codes.OK:
            return _response(self.merge_status, self.merge_body)
        body = self.merge_body or json.dumps({"id": _PR_IID, "state": "merged", "merge_commit_sha": self.merge_sha})
        return _response(httpx.codes.OK, body)


class _FailingClient(_GitLabApiStub):
    """Every read raises — the forge is unreachable / unauthorised."""

    def get_json(self, endpoint: str) -> object:
        self.calls.append(endpoint)
        message = "401 Unauthorized"
        raise httpx.HTTPStatusError(
            message,
            request=httpx.Request("GET", f"https://gitlab.example/{endpoint}"),
            response=httpx.Response(httpx.codes.UNAUTHORIZED),
        )


class _PipelinesClient(_GitLabApiStub):
    """Serves a hand-written pipeline list; every other read is the default MR."""

    def __init__(self, pipelines: list[dict[str, object]]) -> None:
        super().__init__()
        self._pipelines = pipelines

    def get_json(self, endpoint: str) -> object:
        if "/pipelines" in endpoint:
            self.calls.append(endpoint)
            return self._pipelines
        return super().get_json(endpoint)


class _ScriptedMergeClient(_GitLabApiStub):
    """Answers each merge PUT from a scripted queue — the retry-sequence stub."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        super().__init__()
        self._responses = iter(responses)

    def put_response(
        self,
        endpoint: str,
        payload: dict[str, object] | None = None,
        *,
        idempotent: bool = True,
    ) -> httpx.Response:
        self.calls.append(endpoint)
        self.merge_payloads.append(dict(payload or {}))
        return next(self._responses)


def _patch_gitlab(client: object) -> AbstractContextManager[object]:
    """Bind every ``GitLabCodeHost`` built inside the block to *client*."""
    return patch("teatree.backends.gitlab.client.get_client", return_value=client)


def _merge_at(expected_head_oid: str) -> str:
    return execute_bound_merge(
        ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
        expected_head_oid=expected_head_oid,
    )


def _make_ticket(*, gitlab: bool = True) -> Ticket:
    return Ticket.objects.create(
        overlay="acme",
        state=Ticket.State.IN_REVIEW,
        issue_url=_GITLAB_ISSUE_URL if gitlab else "https://github.com/souliane/teatree/issues/1",
    )


class TestFetchLiveHeadShaGitLab(TestCase):
    def test_uses_the_mr_rest_endpoint(self) -> None:
        stub = _GitLabApiStub(sha=_SHA)
        with _patch_gitlab(stub):
            result = _gitlab_query().live_head_sha()
        assert result == _SHA
        # The URL-encoded project slug must appear in the API path.
        assert f"projects/acme%2Fwidget/merge_requests/{_PR_IID}" in stub.calls, (
            f"expected the encoded MR endpoint, got {stub.calls}"
        )

    def test_returns_empty_on_failure(self) -> None:
        with _patch_gitlab(_FailingClient()):
            assert _gitlab_query().live_head_sha() == ""

    def test_returns_empty_on_malformed_json(self) -> None:
        class _Bad(_GitLabApiStub):
            def get_json(self, endpoint: str) -> object:
                message = "Expecting property name"
                raise json.JSONDecodeError(message, "{not json", 1)

        with _patch_gitlab(_Bad()):
            assert _gitlab_query().live_head_sha() == ""


class TestFetchRequiredChecksGitLab(TestCase):
    def test_pipeline_success_maps_to_green(self) -> None:
        with _patch_gitlab(_GitLabApiStub(pipeline_status="success")):
            assert _gitlab_query().required_checks_status() == "green"

    def test_pipeline_running_maps_to_pending(self) -> None:
        with _patch_gitlab(_GitLabApiStub(pipeline_status="running")):
            assert _gitlab_query().required_checks_status() == "pending"

    def test_pipeline_failed_maps_to_failed(self) -> None:
        with _patch_gitlab(_GitLabApiStub(pipeline_status="failed")):
            assert _gitlab_query().required_checks_status() == "failed"

    def test_no_pipeline_is_pending(self) -> None:
        with _patch_gitlab(_PipelinesClient([])):
            # No pipeline ran => NOT proof the required jobs passed => pending (fail
            # closed): an empty pipeline list must never merge as "all checks passed".
            assert _gitlab_query().required_checks_status() == "pending"

    def test_pipeline_query_failure_returns_failed(self) -> None:
        with _patch_gitlab(_FailingClient()):
            assert _gitlab_query().required_checks_status() == "failed"

    def test_required_status_check_contexts_is_empty_no_separate_gate(self) -> None:
        # GitLab gates on the pipeline-status verdict, not branch-protection
        # required-status-check contexts — the host method returns [] (no gate).
        from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

        host = GitLabCodeHost(token="x", base_url="https://gitlab.com/api/v4")
        assert host.fetch_required_status_check_contexts(slug=_GITLAB_SLUG, pr_id=_PR_IID) == []

    def test_selects_head_sha_pipeline_ignoring_canceled_merge_train(self) -> None:
        # The pipelines endpoint interleaves a canceled merge-train pipeline
        # ahead of the real head-branch pipeline; selecting pipelines[0] would
        # misread a green MR as failed and brick the keystone merge gate.
        train_sha = "b" * 40
        pipelines = [
            {
                "id": 999,
                "status": "canceled",
                "sha": train_sha,
                "ref": f"refs/merge-requests/{_PR_IID}/train",
                "source": "merge_train",
            },
            {"id": 100, "status": "success", "sha": _SHA, "source": "merge_request_event"},
        ]

        with _patch_gitlab(_PipelinesClient(pipelines)):
            assert _gitlab_query().required_checks_status() == "green"


class TestExecuteBoundMergeGitLab(TestCase):
    def setUp(self) -> None:
        # The #2829 merge-verdict gate at the top of execute_bound_merge needs a
        # non-stale, independent merge_safe verdict at the bound head.
        seed_merge_safe_verdict(slug=_GITLAB_SLUG, pr_id=_PR_IID, sha=_SHA)

    def test_uses_the_put_merge_endpoint_bound_to_the_sha(self) -> None:
        stub = _GitLabApiStub(merge_sha="commit-sha-12345")
        with _patch_gitlab(stub):
            result = _merge_at(_SHA)
        assert result == "commit-sha-12345"
        assert f"projects/acme%2Fwidget/merge_requests/{_PR_IID}/merge" in stub.calls
        assert stub.merge_payloads == [{"sha": _SHA, "squash": True}]

    def test_merge_failure_raises_precondition_error(self) -> None:
        # HTTP 422 is a policy refusal — a verdict on the merge, never retried.
        refused = _GitLabApiStub(
            merge_status=httpx.codes.UNPROCESSABLE_ENTITY,
            merge_body=json.dumps({"message": "Branch cannot be merged"}),
        )
        with _patch_gitlab(refused), pytest.raises(MergePreconditionError):
            _merge_at(_SHA)

    def test_head_moved_raises_head_moved_error(self) -> None:
        moved = _GitLabApiStub(
            merge_status=httpx.codes.CONFLICT,
            merge_body=json.dumps({"message": "SHA does not match HEAD of source branch"}),
        )
        with _patch_gitlab(moved), pytest.raises(MergeHeadMovedError):
            _merge_at(_SHA)

    def test_transient_response_is_retried_then_succeeds(self) -> None:
        transient_then_ok = _ScriptedMergeClient(
            [
                _response(httpx.codes.BAD_GATEWAY, ""),
                _response(httpx.codes.OK, json.dumps({"merge_commit_sha": "gitlab-merged-0"})),
            ],
        )
        with patch("teatree.core.merge.execution.time.sleep"), _patch_gitlab(transient_then_ok):
            result = _merge_at(_SHA)
        assert result == "gitlab-merged-0"
        assert len(transient_then_ok.merge_payloads) == 2, "the GitLab transient response was not retried to success"

    def test_non_dict_merge_response_falls_back_to_expected_head(self) -> None:
        with _patch_gitlab(_GitLabApiStub(merge_body="[1, 2, 3]")):
            assert _merge_at(_SHA) == _SHA

    def test_unparseable_merge_response_falls_back_to_expected_head(self) -> None:
        # 2xx but a non-JSON body (success, garbled payload): fall back to
        # the bound expected_head_oid rather than crashing.
        with _patch_gitlab(_GitLabApiStub(merge_body="not-json-at-all")):
            assert _merge_at(_SHA) == _SHA


class TestGitLabEndToEndMerge(TestCase):
    """One integration test: full ``merge_ticket_pr`` over a GitLab MR.

    Stubs the GitLab HTTP client only. Walks the entire §17.4.3 chain:
    CodeHostQuery.live_head_sha → pr_is_draft → required_checks_status
    → execute_bound_merge → record_merge_and_advance.
    """

    def test_full_keystone_drives_gitlab_mr_over_http(self) -> None:
        ticket = _make_ticket(gitlab=True)
        clear = _clear(ticket)
        seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
        stub = _GitLabApiStub(sha=_SHA, draft=False, pipeline_status="success")

        with _patch_gitlab(stub):
            outcome: MergeOutcome = merge_ticket_pr(
                clear=clear,
                executing_loop_identity="merge-loop",
            )

        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.merged_sha == stub.merge_sha
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        assert MergeAudit.objects.filter(clear=clear).exists()
        # The GitLab API path must have been reached; the ``gh`` runner was never
        # patched, so a GitHub-branch regression would fail loudly rather than pass.
        assert any("merge_requests" in call for call in stub.calls)


_DRAFT_PROBE = "teatree.backends.gitlab.client.GitLabCodeHost.fetch_pr_draft_state"


class TestHostKindDetection(TestCase):
    """The CLEAR's ticket ``issue_url`` selects the transport."""

    def test_gitlab_com_issue_url_resolves_to_gitlab(self) -> None:
        ticket = _make_ticket(gitlab=True)
        clear = _clear(ticket)
        assert resolve_host_kind(clear, repo_slug=_GITLAB_SLUG) == "gitlab"

    def test_self_hosted_gitlab_issue_url_resolves_to_gitlab(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            state=Ticket.State.IN_REVIEW,
            issue_url=_GITLAB_SELF_HOSTED_URL,
        )
        clear = _clear(ticket)
        assert resolve_host_kind(clear, repo_slug=_GITLAB_SLUG) == "gitlab"

    def test_github_issue_url_resolves_to_github(self) -> None:
        ticket = _make_ticket(gitlab=False)
        clear = _clear(ticket, slug="souliane/teatree", pr_id=1)
        assert resolve_host_kind(clear, repo_slug="souliane/teatree") == "github"

    def test_missing_issue_url_no_longer_defaults_to_github(self) -> None:
        # The legacy ``or "github"`` default bound the GitHub transport against
        # a GitLab MR for every ticketless CLEAR. An unresolvable forge now
        # fails loud; the sanctioned escape is the recorded ``host_kind``.
        clear = MergeClear.objects.create(
            ticket=None,
            pr_id=1,
            slug=_GITLAB_SLUG,
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            pytest.raises(MergePreconditionError, match="could not resolve the forge"),
        ):
            resolve_host_kind(clear, repo_slug=_GITLAB_SLUG)


class TestFetchPrDraftStateGitLab(TestCase):
    """The draft read is the one GitLab keystone probe on the HTTP transport.

    It also gates the review-request broadcast, which runs headless where no
    ``glab`` binary exists — see ``backends.gitlab.client.fetch_pr_draft_state``.
    The forge-payload mapping lives in ``test_gitlab_code_host.py``; here we pin
    that the keystone query reaches it and that an UNREADABLE probe holds the
    merge instead of waving it through.
    """

    def test_draft_refuses_at_the_floor(self) -> None:
        with (
            _http_draft_state(DraftState.DRAFT),
            pytest.raises(MergePreconditionError, match="is in draft state"),
        ):
            assert_not_draft(_gitlab_query())

    def test_confirmed_non_draft_clears_the_floor(self) -> None:
        with _http_draft_state(DraftState.NOT_DRAFT):
            assert_not_draft(_gitlab_query())

    def test_unreadable_draft_state_holds_the_merge(self) -> None:
        with (
            _http_draft_state(DraftState.UNKNOWN),
            pytest.raises(MergePreconditionError, match="could not be read from the forge"),
        ):
            assert_not_draft(_gitlab_query())
