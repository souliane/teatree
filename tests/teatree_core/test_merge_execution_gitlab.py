"""GitLab transport for the §17.4 keystone merge (sibling of test_merge_execution.py).

The §17.4.3 live-forge reads (:meth:`CodeHostQuery.live_head_sha`,
:meth:`CodeHostQuery.pr_draft_state`, :meth:`CodeHostQuery.required_checks_status`)
and ``execute_bound_merge`` originally hardcoded ``gh pr view`` / ``gh api``,
which left GitLab MRs unreachable through the sanctioned path. These
tests assert that each read dispatches by code-host kind (carried on the
:class:`PrRef`'s ``host_kind``) and invokes the equivalent ``glab api`` call —
except the draft read, which GitLab answers over the HTTP API so the headless
image (no ``glab`` binary) can answer it at all.

Only the unstoppable externals — the ``glab`` / ``gh`` subprocess and the GitLab
HTTP client — are stubbed; every teatree model / FSM / DB write is real.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

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


class _GlabStub:
    """Scripted ``glab`` responses keyed by URL substring; records argv per call."""

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def __init__(  # noqa: PLR0913 — test stub mirrors the response surface; each field models one wire-API field.
        self,
        *,
        sha: str = _SHA,
        state: str = "opened",
        pipeline_status: str = "success",
        jobs: list[dict[str, str]] | None = None,
        merge_rc: int = 0,
        merge_sha: str = "merged0deadbeef",
    ) -> None:
        self.sha = sha
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


class TestFetchLiveHeadShaGitLab(TestCase):
    def test_uses_glab_api_merge_request_endpoint(self) -> None:
        stub = _GlabStub(sha=_SHA)
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            result = _gitlab_query().live_head_sha()
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
            assert _gitlab_query().live_head_sha() == ""

    def test_returns_empty_on_malformed_json(self) -> None:
        def _bad(argv: list[str]) -> tuple[int, str, str]:
            return (0, "{not json", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_bad):
            assert _gitlab_query().live_head_sha() == ""


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


class TestFetchRequiredChecksGitLab(TestCase):
    def test_pipeline_success_maps_to_green(self) -> None:
        stub = _GlabStub(pipeline_status="success")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert _gitlab_query().required_checks_status() == "green"

    def test_pipeline_running_maps_to_pending(self) -> None:
        stub = _GlabStub(pipeline_status="running")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert _gitlab_query().required_checks_status() == "pending"

    def test_pipeline_failed_maps_to_failed(self) -> None:
        stub = _GlabStub(pipeline_status="failed")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            assert _gitlab_query().required_checks_status() == "failed"

    def test_no_pipeline_is_pending(self) -> None:
        def _no_pipeline(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/pipelines" in joined:
                return (0, "[]", "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_no_pipeline):
            # No pipeline ran => NOT proof the required jobs passed => pending (fail
            # closed): an empty pipeline list must never merge as "all checks passed".
            assert _gitlab_query().required_checks_status() == "pending"

    def test_pipeline_query_failure_returns_failed(self) -> None:
        def _boom(argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "auth error")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_boom):
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

        def _train_then_head(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "/pipelines" in joined:
                return (0, json.dumps(pipelines), "")
            if "/merge_requests/" in joined:
                return (0, json.dumps({"iid": _PR_IID, "sha": _SHA}), "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_train_then_head):
            assert _gitlab_query().required_checks_status() == "green"


class TestExecuteBoundMergeGitLab(TestCase):
    def setUp(self) -> None:
        # The #2829 merge-verdict gate at the top of execute_bound_merge needs a
        # non-stale, independent merge_safe verdict at the bound head.
        seed_merge_safe_verdict(slug=_GITLAB_SLUG, pr_id=_PR_IID, sha=_SHA)

    def test_uses_glab_api_put_merge_endpoint_with_sha(self) -> None:
        stub = _GlabStub(merge_sha="commit-sha-12345")
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub):
            result = execute_bound_merge(
                ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
                expected_head_oid=_SHA,
            )
        assert result == "commit-sha-12345"
        # The #18 not-draft/CI floor also reads /merge_requests/<iid>[/pipelines]
        # (both contain "merge_requests"); the bound-merge PUT is the one argv
        # carrying "PUT" — select it specifically.
        merge_calls = [c for c in stub.calls if "PUT" in c]
        assert merge_calls, f"expected at least one merge PUT call, got {stub.calls}"
        joined = " ".join(merge_calls[0])
        assert "/merge" in joined
        assert _SHA in joined

    def test_merge_failure_raises_precondition_error(self) -> None:
        stub = _GlabStub(merge_rc=1)
        with (
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub),
            pytest.raises(MergePreconditionError),
        ):
            execute_bound_merge(
                ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
                expected_head_oid=_SHA,
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
                ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
                expected_head_oid=_SHA,
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
            # #18: the FAILED-live-CI floor re-reads the head pipeline — a green
            # head pipeline at _SHA keeps the merge proceeding.
            if "/pipelines" in joined:
                return (0, json.dumps([{"id": 1, "status": "success", "sha": _SHA}]), "")
            # Pre-retry merge-state probe (and the not-draft read): still OPEN.
            if "/merge_requests/" in joined:
                return (0, json.dumps({"iid": _PR_IID, "state": "opened", "sha": _SHA, "draft": False}), "")
            return (0, "", "")

        with (
            patch("teatree.core.merge.execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=_transient_then_ok),
        ):
            result = execute_bound_merge(
                ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
                expected_head_oid=_SHA,
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
                ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
                expected_head_oid=_SHA,
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
                ref=PrRef(slug=_GITLAB_SLUG, pr_id=_PR_IID, host_kind="gitlab"),
                expected_head_oid=_SHA,
            )
        assert result == _SHA


class TestGitLabEndToEndMerge(TestCase):
    """One integration test: full ``merge_ticket_pr`` over a GitLab MR.

    Stubs ``_run_glab`` only. Walks the entire §17.4.3 chain:
    CodeHostQuery.live_head_sha → pr_draft_state → required_checks_status
    → execute_bound_merge → record_merge_and_advance.
    """

    def test_full_keystone_drives_gitlab_mr_via_glab(self) -> None:
        ticket = _make_ticket(gitlab=True)
        clear = _clear(ticket)
        seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
        stub = _GlabStub(sha=_SHA, pipeline_status="success")

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
