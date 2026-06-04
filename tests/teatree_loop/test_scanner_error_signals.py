"""Scanner auth/rate-limit/missing-scope failures must raise, never fail-open (#1287).

Codex review of #1282 flagged that the PR-sweep, GitLab-approvals, and Slack
``fetch_channel_history`` paths all convert upstream failures into empty
returns. The dispatcher reads empty as "nothing to do", so a forge auth
failure silently suppresses all signals for that scanner.

The fix shape: reserve empty for genuinely empty data; raise
``ScannerError`` on auth / rate-limit / missing-scope / network failures
so the dispatcher's existing ``_run_job`` catcher records the error,
notifies the user, and continues with the other scanners for the tick.

These tests are the RED guard for the three scanners cited in the codex
finding plus a dispatcher-level test that proves ``ScannerError``
propagates cleanly into ``report.errors`` without crashing the tick.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase

from teatree.backends import slack_http
from teatree.backends.protocols import ApprovalState, ReviewState
from teatree.backends.slack_bot import SlackBotBackend
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.loop.scanners.gitlab_approvals import GitLabApprovalsScanner
from teatree.loop.scanners.pr_sweep import PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient, NullMergeNotifier
from teatree.loop.tick import TickRequest, run_tick
from teatree.types import RawAPIDict

# ---------------------------------------------------------------------------
# PR sweep — gh auth/rate-limit failures must propagate as ScannerError.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class TestPrSweepGhApiClientAuthFailure:
    """``GhPrApiClient.list_open_prs`` must raise on auth/rate-limit failure."""

    def test_gh_auth_failure_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(
                returncode=1,
                stdout="",
                stderr=(
                    "gh: To get started with GitHub CLI, please run:  gh auth login\n"
                    "Alternatively, populate the GH_TOKEN environment variable with a "
                    "GitHub API authentication token.\n"
                ),
            )

        monkeypatch.setattr("teatree.loop.scanners.pr_sweep_adapters.run_allowed_to_fail", _stub_run)
        api = GhPrApiClient(token="")
        with pytest.raises(ScannerError) as excinfo:
            api.list_open_prs(slug="owner/repo")
        assert excinfo.value.error_class == ScannerErrorClass.AUTH

    def test_gh_rate_limit_failure_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(
                returncode=1,
                stdout="",
                stderr="API rate limit exceeded for user ID 12345.",
            )

        monkeypatch.setattr("teatree.loop.scanners.pr_sweep_adapters.run_allowed_to_fail", _stub_run)
        api = GhPrApiClient(token="x")
        with pytest.raises(ScannerError) as excinfo:
            api.list_open_prs(slug="owner/repo")
        assert excinfo.value.error_class == ScannerErrorClass.RATE_LIMIT

    def test_gh_not_installed_still_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``FileNotFoundError`` (gh not installed) keeps the pre-existing empty fallback.

        It is an environmental error, not an upstream auth/rate-limit
        failure — the dispatcher would only get noise from a tick-per-tick
        ScannerError, so the empty-list path stays the right call.
        """

        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            msg = "gh"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("teatree.loop.scanners.pr_sweep_adapters.run_allowed_to_fail", _stub_run)
        api = GhPrApiClient(token="x")
        assert api.list_open_prs(slug="owner/repo") == []

    def test_gh_returns_genuinely_empty_list_no_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returncode 0 with ``[]`` JSON output is the "no PRs" case — empty, no error.

        Reserve empty for genuinely empty data; raise only on upstream
        failure (#1287).
        """

        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=0, stdout="[]", stderr="")

        monkeypatch.setattr("teatree.loop.scanners.pr_sweep_adapters.run_allowed_to_fail", _stub_run)
        api = GhPrApiClient(token="x")
        assert api.list_open_prs(slug="owner/repo") == []


class TestPrSweepScannerPropagatesError:
    """``PrSweepScanner._safe_list`` must propagate ``ScannerError`` to the dispatcher."""

    def test_scanner_propagates_scanner_error_from_api(self) -> None:
        class _AuthFailingApi:
            def list_open_prs(self, *, slug: str) -> list[Any]:
                _ = slug
                raise ScannerError(
                    scanner="pr_sweep",
                    error_class=ScannerErrorClass.AUTH,
                    detail="gh auth login required",
                )

            def main_check_failed(self, *, slug: str, check_name: str) -> bool:
                _ = (slug, check_name)
                return False

            def merge_pr_squash(self, *, slug: str, pr_id: int) -> tuple[bool, str]:
                _ = (slug, pr_id)
                return False, ""

        class _NullKeystone:
            def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
                _ = clear_id
                return False, "", ""

        scanner = PrSweepScanner(
            repos=("owner/repo",),
            api=_AuthFailingApi(),
            keystone=_NullKeystone(),
            notifier=NullMergeNotifier(),
            overlay="t",
        )
        with pytest.raises(ScannerError) as excinfo:
            scanner.scan()
        assert excinfo.value.error_class == ScannerErrorClass.AUTH


# ---------------------------------------------------------------------------
# GitLab approvals — HTTP 401 (or any non-NotImplementedError exception) must
# propagate as ScannerError.
# ---------------------------------------------------------------------------


@dataclass
class _AuthFailingCodeHost:
    """In-memory ``CodeHostBackend`` whose ``get_mr_approvals`` raises an auth error."""

    user: str = "alice"
    prs: list[RawAPIDict] = field(default_factory=list)
    approval_call_count: int = 0

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self.prs

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return []

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

    def create_pr(self, spec: Any) -> RawAPIDict:
        _ = spec
        return {}

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        _ = (repo, filepath)
        return {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        _ = (issue_url, body)
        return {}

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        _ = (repo, pr_iid)
        self.approval_call_count += 1
        # Simulate a GitLab 401 surfacing as an httpx HTTPStatusError.
        request = httpx.Request("GET", "https://gitlab.com/api/v4/projects/.../approvals")
        response = httpx.Response(401, request=request, json={"message": "401 Unauthorized"})
        msg = "401 Unauthorized"
        raise httpx.HTTPStatusError(msg, request=request, response=response)


def _gitlab_mr(*, iid: int = 42, sha: str = "deadbeef", project: str = "acme/backend") -> RawAPIDict:
    return {
        "iid": iid,
        "title": "Add widget",
        "web_url": f"https://gitlab.com/{project}/-/merge_requests/{iid}",
        "sha": sha,
        "target_branch": "main",
        "state": "opened",
    }


class TestGitLabApprovalsScannerAuthFailure(TestCase):
    """``GitLabApprovalsScanner`` must raise ``ScannerError`` on a GitLab 401."""

    def test_gitlab_401_raises_scanner_error(self) -> None:
        host = _AuthFailingCodeHost(prs=[_gitlab_mr(iid=42, sha="abc123")])
        scanner = GitLabApprovalsScanner(host=host)

        with pytest.raises(ScannerError) as excinfo:
            scanner.scan()

        assert excinfo.value.error_class == ScannerErrorClass.AUTH
        # The scanner must have CALLED the backend — silent skip would also
        # leave the call count at 0; this guards against a regression that
        # short-circuits on the URL filter before reaching the backend.
        assert host.approval_call_count == 1


# ---------------------------------------------------------------------------
# Slack ``fetch_channel_history`` — missing_scope / invalid_auth / ratelimited
# must raise, not silently return [].
# ---------------------------------------------------------------------------


def _slack_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "x"))


class TestSlackFetchChannelHistoryAuthFailure:
    def test_missing_scope_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            _ = (url, kwargs)
            return _slack_response({"ok": False, "error": "missing_scope"})

        # _channel_token consults conversations.info via _post; stub it too.
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        monkeypatch.setattr(
            slack_http.httpx,
            "post",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "missing_scope"}),
        )
        backend = SlackBotBackend(bot_token="xoxb-test")

        with pytest.raises(ScannerError) as excinfo:
            backend.fetch_channel_history(channel="C42", limit=10)
        assert excinfo.value.error_class == ScannerErrorClass.MISSING_SCOPE

    def test_invalid_auth_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            slack_http.httpx,
            "get",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "invalid_auth"}),
        )
        monkeypatch.setattr(
            slack_http.httpx,
            "post",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "invalid_auth"}),
        )
        backend = SlackBotBackend(bot_token="xoxb-test")

        with pytest.raises(ScannerError) as excinfo:
            backend.fetch_channel_history(channel="C42", limit=10)
        assert excinfo.value.error_class == ScannerErrorClass.AUTH

    def test_rate_limited_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            slack_http.httpx,
            "get",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "ratelimited"}),
        )
        monkeypatch.setattr(
            slack_http.httpx,
            "post",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "ratelimited"}),
        )
        backend = SlackBotBackend(bot_token="xoxb-test")

        with pytest.raises(ScannerError) as excinfo:
            backend.fetch_channel_history(channel="C42", limit=10)
        assert excinfo.value.error_class == ScannerErrorClass.RATE_LIMIT

    def test_channel_not_found_still_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``channel_not_found`` stays empty-and-quiet — only token-global failures raise.

        Preserves the explicit "one slow channel never breaks the scan
        loop" carve-out from #1255. Only global token failures
        (auth/scope/ratelimit) raise.
        """
        monkeypatch.setattr(
            slack_http.httpx,
            "get",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "channel_not_found"}),
        )
        monkeypatch.setattr(
            slack_http.httpx,
            "post",
            lambda url, **kwargs: _slack_response({"ok": False, "error": "channel_not_found"}),
        )
        backend = SlackBotBackend(bot_token="xoxb-test")

        assert backend.fetch_channel_history(channel="C42", limit=10) == []


# ---------------------------------------------------------------------------
# Dispatcher — a raising scanner must NOT crash the tick. Other scanners still
# run; the error appears in ``report.errors``; the user is DM'd once.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _GoodScanner:
    name: str = "good"

    def scan(self) -> list[ScanSignal]:
        return [ScanSignal(kind="my_pr.open", summary="hi")]


@dataclass(slots=True)
class _AuthFailingScanner:
    name: str = "auth_failing"

    def scan(self) -> list[ScanSignal]:
        raise ScannerError(
            scanner=self.name,
            error_class=ScannerErrorClass.AUTH,
            detail="401 Unauthorized",
        )


class TestDispatcherHandlesScannerError:
    def test_scanner_error_recorded_other_scanners_unaffected(self, tmp_path: Path) -> None:
        good = _GoodScanner()
        bad = _AuthFailingScanner()
        statusline = tmp_path / "statusline.txt"

        report = run_tick(
            TickRequest(scanners=[good, bad]),
            statusline_path=statusline,
        )

        # The good scanner's signal must be present.
        assert report.signal_count == 1
        # The bad scanner's error must be recorded — the dispatcher must NOT
        # have crashed; the tick must complete.
        assert any("auth_failing" in label for label in report.errors)
        recorded = next(v for k, v in report.errors.items() if "auth_failing" in k)
        assert "auth" in recorded.lower()

    def test_scanner_error_notifies_user_once_per_class_per_day(
        self,
        tmp_path: Path,
    ) -> None:
        """``_run_job`` DMs the user on a caught ``ScannerError``, idempotency-keyed.

        Key shape is ``(scanner, error_class, UTC date)`` so a sustained
        failure does not spam one DM per tick.
        """
        bad = _AuthFailingScanner()
        statusline = tmp_path / "statusline.txt"

        with patch("teatree.loop.tick_jobs.notify_with_fallback") as mock_notify:
            run_tick(TickRequest(scanners=[bad]), statusline_path=statusline)
            run_tick(TickRequest(scanners=[bad]), statusline_path=statusline)

        # Both ticks called the verified-delivery wrapper; idempotency happens
        # INSIDE the notify path (via BotPing). The contract here is only that
        # the dispatcher invokes the helper with a stable key per (scanner,
        # class, day) — actual dedup is the notify path's job and already
        # covered by its own tests.
        assert mock_notify.call_count >= 1
        first_call = mock_notify.call_args_list[0]
        idempotency_key = first_call.kwargs.get("idempotency_key", "")
        assert "auth_failing" in idempotency_key
        assert "auth" in idempotency_key
