"""GitLab transport for the §17.4 keystone merge (sibling of test_merge_execution.py).

The three §17.4.3 fetch helpers (``fetch_live_head_sha``,
``fetch_pr_is_draft``, ``fetch_required_checks_status``) and
``execute_bound_merge`` originally hardcoded ``gh pr view`` / ``gh api``,
which left GitLab MRs unreachable through the sanctioned path. These
tests assert that each helper dispatches by code-host kind (detected
from the CLEAR's ticket ``issue_url``) and invokes the equivalent
``glab api`` call.

Only the unstoppable external — the ``glab`` / ``gh`` subprocess — is
stubbed; every teatree model / FSM / DB write is real.
"""

import json
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core import merge_execution
from teatree.core.merge_execution import (
    MergeHeadMovedError,
    MergeOutcome,
    MergePreconditionError,
    execute_bound_merge,
    fetch_live_head_sha,
    fetch_pr_is_draft,
    fetch_required_checks_status,
    merge_ticket_pr,
)
from teatree.core.models import MergeAudit, MergeClear, Ticket

pytestmark = pytest.mark.django_db

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


class _GlabStub:
    """Scripted ``glab`` responses keyed by URL substring; records argv per call."""

    def __init__(  # noqa: PLR0913 — test stub mirrors the response surface; each field models one wire-API field.
        self,
        *,
        sha: str = _SHA,
        draft: bool = False,
        state: str = "opened",
        pipeline_status: str = "success",
        jobs: list[dict[str, str]] | None = None,
        merge_rc: int = 0,
        merge_sha: str = "merged0deadbeef",
    ) -> None:
        self.sha = sha
        self.draft = draft
        self.state = state
        self.pipeline_status = pipeline_status
        self.jobs = jobs if jobs is not None else [{"status": "success"}]
        self.merge_rc = merge_rc
        self.merge_sha = merge_sha
        self.calls: list[list[str]] = []

    def _mr_payload(self) -> str:
        return json.dumps(
            {
                "iid": _PR_IID,
                "sha": self.sha,
                "draft": self.draft,
                "state": self.state,
            },
        )

    def _pipelines_payload(self) -> str:
        return json.dumps([{"id": 12345, "status": self.pipeline_status, "sha": self.sha}])

    def _jobs_payload(self) -> str:
        return json.dumps(self.jobs)

    def _merge_payload(self) -> str:
        return json.dumps({"id": _PR_IID, "state": "merged", "merge_commit_sha": self.merge_sha})

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        # PUT .../merge_requests/<iid>/merge
        if "/merge" in joined and ("PUT" in argv or ("-X" in argv and "PUT" in joined)):
            if self.merge_rc != 0:
                return (1, "", "merge failed (409)")
            return (0, self._merge_payload(), "")
        if "/pipelines/" in joined and "/jobs" in joined:
            return (0, self._jobs_payload(), "")
        if "/pipelines" in joined:
            return (0, self._pipelines_payload(), "")
        # Bare MR endpoint .../merge_requests/<iid>
        if "/merge_requests/" in joined:
            return (0, self._mr_payload(), "")
        return (0, "", "")


def _make_ticket(*, gitlab: bool = True) -> Ticket:
    return Ticket.objects.create(
        overlay="acme",
        state=Ticket.State.IN_REVIEW,
        issue_url=_GITLAB_ISSUE_URL if gitlab else "https://github.com/souliane/teatree/issues/1",
    )


class TestHostKindDetection(TestCase):
    """The CLEAR's ticket ``issue_url`` selects the transport."""

    def test_gitlab_com_issue_url_resolves_to_gitlab(self) -> None:
        ticket = _make_ticket(gitlab=True)
        clear = _clear(ticket)
        assert merge_execution._resolve_host_kind(clear) == "gitlab"

    def test_self_hosted_gitlab_issue_url_resolves_to_gitlab(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            state=Ticket.State.IN_REVIEW,
            issue_url=_GITLAB_SELF_HOSTED_URL,
        )
        clear = _clear(ticket)
        assert merge_execution._resolve_host_kind(clear) == "gitlab"

    def test_github_issue_url_resolves_to_github(self) -> None:
        ticket = _make_ticket(gitlab=False)
        clear = _clear(ticket, slug="souliane/teatree", pr_id=1)
        assert merge_execution._resolve_host_kind(clear) == "github"

    def test_missing_issue_url_defaults_to_github(self) -> None:
        # Back-compat: a CLEAR without a ticket / without an issue_url
        # keeps the legacy ``gh`` transport so existing GitHub callers
        # are not regressed.
        clear = MergeClear.objects.create(
            ticket=None,
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )
        assert merge_execution._resolve_host_kind(clear) == "github"


class TestFetchLiveHeadShaGitLab(TestCase):
    def test_uses_glab_api_merge_request_endpoint(self) -> None:
        stub = _GlabStub(sha=_SHA)
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            result = fetch_live_head_sha(_GITLAB_SLUG, _PR_IID, host_kind="gitlab")
        assert result == _SHA
        assert any(f"merge_requests/{_PR_IID}" in " ".join(call) for call in stub.calls), (
            f"expected an MR endpoint call, got {stub.calls}"
        )
        # The URL-encoded project slug must appear in the API path.
        encoded = "acme%2Fwidget"
        assert any(encoded in " ".join(call) for call in stub.calls), f"expected encoded slug in {stub.calls}"

    def test_returns_empty_on_failure(self) -> None:
        def _boom(argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "auth error")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_boom):
            assert fetch_live_head_sha(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == ""

    def test_returns_empty_on_malformed_json(self) -> None:
        def _bad(argv: list[str]) -> tuple[int, str, str]:
            return (0, "{not json", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_bad):
            assert fetch_live_head_sha(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == ""


class TestFetchPrIsDraftGitLab(TestCase):
    def test_draft_true_when_mr_draft(self) -> None:
        stub = _GlabStub(draft=True)
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert fetch_pr_is_draft(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") is True

    def test_draft_false_when_mr_not_draft(self) -> None:
        stub = _GlabStub(draft=False)
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert fetch_pr_is_draft(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") is False

    def test_draft_false_on_api_failure(self) -> None:
        def _boom(argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "boom")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_boom):
            assert fetch_pr_is_draft(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") is False


class TestFetchRequiredChecksGitLab(TestCase):
    def test_pipeline_success_maps_to_green(self) -> None:
        stub = _GlabStub(pipeline_status="success")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert fetch_required_checks_status(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == "green"

    def test_pipeline_running_maps_to_pending(self) -> None:
        stub = _GlabStub(pipeline_status="running")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert fetch_required_checks_status(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == "pending"

    def test_pipeline_failed_maps_to_failed(self) -> None:
        stub = _GlabStub(pipeline_status="failed")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert fetch_required_checks_status(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == "failed"

    def test_no_pipeline_is_green(self) -> None:
        def _no_pipeline(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/pipelines" in joined:
                return (0, "[]", "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_no_pipeline):
            # No pipelines => no required checks => green (mirrors GitHub
            # rollup-empty behaviour which the GitHub branch returns).
            assert fetch_required_checks_status(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == "green"

    def test_pipeline_query_failure_returns_failed(self) -> None:
        def _boom(argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "auth error")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_boom):
            assert fetch_required_checks_status(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == "failed"

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

        def _train_then_head(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/pipelines" in joined:
                return (0, json.dumps(pipelines), "")
            if "/merge_requests/" in joined:
                return (0, json.dumps({"iid": _PR_IID, "sha": _SHA}), "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_train_then_head):
            assert fetch_required_checks_status(_GITLAB_SLUG, _PR_IID, host_kind="gitlab") == "green"


class TestExecuteBoundMergeGitLab(TestCase):
    def test_uses_glab_api_put_merge_endpoint_with_sha(self) -> None:
        stub = _GlabStub(merge_sha="commit-sha-12345")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            result = execute_bound_merge(
                slug=_GITLAB_SLUG,
                pr_id=_PR_IID,
                expected_head_oid=_SHA,
                host_kind="gitlab",
            )
        assert result == "commit-sha-12345"
        merge_calls = [c for c in stub.calls if "merge" in " ".join(c) and "merge_requests" in " ".join(c)]
        assert merge_calls, f"expected at least one merge API call, got {stub.calls}"
        joined = " ".join(merge_calls[0])
        assert "PUT" in joined
        assert _SHA in joined

    def test_merge_failure_raises_precondition_error(self) -> None:
        stub = _GlabStub(merge_rc=1)
        with (
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub),
            pytest.raises(MergePreconditionError),
        ):
            execute_bound_merge(
                slug=_GITLAB_SLUG,
                pr_id=_PR_IID,
                expected_head_oid=_SHA,
                host_kind="gitlab",
            )

    def test_head_moved_raises_head_moved_error(self) -> None:
        def _sha_mismatch(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/merge" in joined and "PUT" in argv:
                return (1, "", "SHA does not match HEAD of source branch (409)")
            return (0, "", "")

        with (
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_sha_mismatch),
            pytest.raises(MergeHeadMovedError),
        ):
            execute_bound_merge(
                slug=_GITLAB_SLUG,
                pr_id=_PR_IID,
                expected_head_oid=_SHA,
                host_kind="gitlab",
            )

    def test_transient_response_is_retried_then_succeeds(self) -> None:
        attempts = {"merge": 0}

        def _transient_then_ok(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/merge" in joined and "PUT" in argv:
                attempts["merge"] += 1
                if attempts["merge"] == 1:
                    return (1, "", "unexpected end of JSON input")
                return (0, json.dumps({"merge_commit_sha": "glab-merged-0"}), "")
            # Pre-retry merge-state probe: still OPEN (the failed call did not land).
            if "/merge_requests/" in joined:
                return (0, json.dumps({"iid": _PR_IID, "state": "opened", "sha": _SHA}), "")
            return (0, "", "")

        with (
            patch("teatree.core.merge_execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_transient_then_ok),
        ):
            result = execute_bound_merge(
                slug=_GITLAB_SLUG,
                pr_id=_PR_IID,
                expected_head_oid=_SHA,
                host_kind="gitlab",
            )
        assert result == "glab-merged-0"
        assert attempts["merge"] == 2, "the GitLab transient response was not retried to success"

    def test_non_dict_merge_response_falls_back_to_expected_head(self) -> None:
        def _list_body(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/merge" in joined and "PUT" in argv:
                return (0, "[1, 2, 3]", "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_list_body):
            result = execute_bound_merge(
                slug=_GITLAB_SLUG,
                pr_id=_PR_IID,
                expected_head_oid=_SHA,
                host_kind="gitlab",
            )
        assert result == _SHA

    def test_unparseable_merge_response_falls_back_to_expected_head(self) -> None:
        # rc 0 but a non-JSON body (success, garbled payload): fall back to
        # the bound expected_head_oid rather than crashing.
        def _garbled_body(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/merge" in joined and "PUT" in argv:
                return (0, "not-json-at-all", "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_garbled_body):
            result = execute_bound_merge(
                slug=_GITLAB_SLUG,
                pr_id=_PR_IID,
                expected_head_oid=_SHA,
                host_kind="gitlab",
            )
        assert result == _SHA


class TestGitLabEndToEndMerge(TestCase):
    """One integration test: full ``merge_ticket_pr`` over a GitLab MR.

    Stubs ``_run_glab`` only. Walks the entire §17.4.3 chain:
    fetch_live_head_sha → fetch_pr_is_draft → fetch_required_checks_status
    → execute_bound_merge → record_merge_and_advance.
    """

    def test_full_keystone_drives_gitlab_mr_via_glab(self) -> None:
        ticket = _make_ticket(gitlab=True)
        clear = _clear(ticket)
        stub = _GlabStub(sha=_SHA, draft=False, pipeline_status="success")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
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
        # The GitLab API path must have been reached; no ``gh`` calls would
        # show up because ``_run_gh`` was never patched and would fail loudly
        # if invoked (no gh available + the GitLab branch never calls it).
        assert any("merge_requests" in " ".join(c) for c in stub.calls)
