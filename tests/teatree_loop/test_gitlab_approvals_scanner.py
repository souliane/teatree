"""Integration tests for ``GitLabApprovalsScanner`` (#936 phase 1).

Real GitLab MR payload shapes (from ``/merge_requests/<iid>``) plus a
real ``Ticket`` row for the idempotency cache. The backend protocol
calls are stubbed by an in-memory ``FakeCodeHost`` that conforms to
``CodeHostBackend`` — no MagicMock-spec mocks (per AGENTS.md test
doctrine).
"""

import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.backend_protocols import ApprovalState, ReviewState
from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.models import Ticket
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.gitlab_approvals import GitLabApprovalsScanner
from teatree.types import RawAPIDict


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` conforming to the protocol — no MagicMock.

    Mirrors the shape of :class:`tests.teatree_loop.test_scanners.FakeCodeHost`
    so the scanner sees the same payload contract production scanners do.
    """

    user: str = "alice"
    my_prs: list[RawAPIDict] = field(default_factory=list)
    approvals: dict[tuple[str, int], ApprovalState] = field(default_factory=dict)
    approval_calls: list[tuple[str, int]] = field(default_factory=list)
    raise_not_implemented: bool = False

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self.my_prs

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
        self.approval_calls.append((repo, pr_iid))
        if self.raise_not_implemented:
            msg = "GitHub stub"
            raise NotImplementedError(msg)
        return self.approvals.get(
            (repo, pr_iid),
            ApprovalState(approvals_left=1, approved_by=[], unresolved_resolvable=0),
        )


def _gitlab_mr(
    *,
    iid: int = 42,
    sha: str = "deadbeef",
    project: str = "acme/backend",
    target: str = "main",
    title: str = "Add widget",
) -> RawAPIDict:
    """Shape mirrors the actual GitLab ``GET /merge_requests`` list payload."""
    return {
        "iid": iid,
        "title": title,
        "web_url": f"https://gitlab.com/{project}/-/merge_requests/{iid}",
        "sha": sha,
        "target_branch": target,
        "state": "opened",
    }


class _StubOverlay:
    """Minimal overlay stub matching the ``can_auto_merge`` surface."""

    def __init__(self, guard: MergeGuard) -> None:
        self._guard = guard

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        _ = (target_ref, thread_ref)
        return self._guard


class TestGitLabApprovalsScanner(TestCase):
    def test_approved_clean_mr_emits_merge_needed(self) -> None:
        """An approved MR with no unresolved threads emits ``merge_needed``."""
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=42, sha="abc123")],
            approvals={
                ("acme/backend", 42): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)

        signals = scanner.scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "incoming_event.merge_needed"
        assert signal.payload["target_ref"] == "main"
        assert signal.payload["thread_ref"] == "https://gitlab.com/acme/backend/-/merge_requests/42"
        assert signal.payload["event_id"] is None
        assert signal.payload["reason"] == "approved"

    def test_approved_with_unresolved_resolvable_emits_merge_blocked(self) -> None:
        """Even with a permissive overlay guard, unresolved threads block merge."""
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=43, sha="def456")],
            approvals={
                ("acme/backend", 43): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=2,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)

        signals = scanner.scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "incoming_event.merge_blocked"
        assert "2 unresolved" in signal.summary
        assert signal.payload["reason"] == "unresolved resolvable threads: 2"
        assert signal.payload["target_ref"] == "main"

    def test_not_approved_emits_nothing(self) -> None:
        """An MR with ``approvals_left > 0`` is steady state — no signal."""
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=44, sha="aaa111")],
            approvals={
                ("acme/backend", 44): ApprovalState(
                    approvals_left=1,
                    approved_by=[],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)

        signals = scanner.scan()

        assert signals == []

    def test_overlay_escalate_emits_merge_escalation(self) -> None:
        """``can_auto_merge`` returning ``allowed=False, escalate=True`` → escalation."""
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=45, sha="bbb222")],
            approvals={
                ("acme/backend", 45): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)
        guard = MergeGuard(allowed=False, reason="human review required", escalate=True)

        with patch.object(overlay_loader_mod, "get_overlay", return_value=_StubOverlay(guard)):
            signals = scanner.scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "incoming_event.merge_escalation"
        assert signal.payload["reason"] == "human review required"
        assert signal.payload["target_ref"] == "main"

    def test_idempotent_emission_same_sha(self) -> None:
        """A second tick at the same head SHA emits nothing — recorded in ``Ticket.extra``.

        ``_record_emission`` only updates *existing* Ticket rows; a real ticket
        must exist for idempotency to function.  The scanner never creates
        phantom blank-overlay rows (F4 fix).
        """
        url = "https://gitlab.com/acme/backend/-/merge_requests/46"
        Ticket.objects.create(issue_url=url, overlay="acme-backend")
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=46, sha="ccc333")],
            approvals={
                ("acme/backend", 46): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)

        first = scanner.scan()
        second = scanner.scan()

        assert len(first) == 1
        assert first[0].kind == "incoming_event.merge_needed"
        assert second == []
        ticket = Ticket.objects.get(issue_url=url)
        assert ticket.extra["last_approval_sha"] == "ccc333"

    def test_new_sha_re_emits(self) -> None:
        """A push (new head SHA) resets the idempotency window — re-emit.

        A real ``Ticket`` row must exist so ``_record_emission`` can store
        the first SHA and ``_already_emitted_at`` can gate the second scan
        correctly.  Without a pre-existing row both scans always emit
        (no-ticket → early-return → no SHA stored), making the test vacuous.
        """
        url = "https://gitlab.com/acme/backend/-/merge_requests/47"
        Ticket.objects.create(issue_url=url, overlay="acme-backend")
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=47, sha="sha-1")],
            approvals={
                ("acme/backend", 47): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)

        first = scanner.scan()
        host.my_prs = [_gitlab_mr(iid=47, sha="sha-2")]
        second = scanner.scan()

        assert len(first) == 1
        assert len(second) == 1
        assert second[0].kind == "incoming_event.merge_needed"
        ticket = Ticket.objects.get(issue_url=url)
        assert ticket.extra["last_approval_sha"] == "sha-2"

    def test_github_backend_silently_skipped(self) -> None:
        """``get_mr_approvals`` raising NotImplementedError → scanner skips the PR."""
        host = FakeCodeHost(
            raise_not_implemented=True,
            my_prs=[_gitlab_mr(iid=48, sha="ddd444")],
        )
        scanner = GitLabApprovalsScanner(host=host)

        signals = scanner.scan()

        assert signals == []
        # The scanner must still have CALLED the backend — silent skip, not
        # short-circuit. This catches a regression where a future "only call
        # GitLab backends" filter forgets to call the unknown ones at all.
        assert host.approval_calls == [("acme/backend", 48)]

    def test_github_url_pattern_skipped_without_backend_call(self) -> None:
        """GitHub PR URLs (``/pull/`` shape) are filtered out before the backend call.

        This keeps a mixed-host overlay from paying a backend round-trip per
        tick to discover that the GitHub backend raises NotImplementedError.
        """
        host = FakeCodeHost(
            my_prs=[
                {
                    "iid": 99,
                    "number": 99,
                    "title": "GitHub PR",
                    "html_url": "https://github.com/o/r/pull/99",
                    "web_url": "https://github.com/o/r/pull/99",
                    "sha": "ghsha",
                    "target_branch": "main",
                },
            ],
        )
        scanner = GitLabApprovalsScanner(host=host)

        signals = scanner.scan()

        assert signals == []
        assert host.approval_calls == []

    def test_no_user_no_signals(self) -> None:
        """Empty user → empty signal list (nothing to scan against)."""
        host = FakeCodeHost(user="", my_prs=[_gitlab_mr()])
        scanner = GitLabApprovalsScanner(host=host)

        assert scanner.scan() == []

    def test_overlay_blocks_emits_merge_blocked(self) -> None:
        """``can_auto_merge`` returning ``allowed=False, escalate=False`` → blocked."""
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=49, sha="fff666")],
            approvals={
                ("acme/backend", 49): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)
        guard = MergeGuard(allowed=False, reason="freeze window", escalate=False)

        with patch.object(overlay_loader_mod, "get_overlay", return_value=_StubOverlay(guard)):
            signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].kind == "incoming_event.merge_blocked"
        assert signals[0].payload["reason"] == "freeze window"

    def test_pr_with_no_url_is_skipped(self) -> None:
        """A PR payload missing ``web_url``/``html_url`` is silently skipped."""
        host = FakeCodeHost(my_prs=[{"iid": 1, "title": "no url"}])
        scanner = GitLabApprovalsScanner(host=host)

        assert scanner.scan() == []
        assert host.approval_calls == []

    def test_pr_with_no_iid_is_skipped(self) -> None:
        """A GitLab URL whose payload is missing ``iid`` is silently skipped."""
        host = FakeCodeHost(
            my_prs=[
                {
                    "web_url": "https://gitlab.com/acme/backend/-/merge_requests/77",
                    "title": "no iid",
                },
            ],
        )
        scanner = GitLabApprovalsScanner(host=host)

        # The URL still embeds the iid, but the payload doesn't surface it as a
        # field — the scanner must use the payload field, not parse the URL.
        # No iid → no backend call.
        assert scanner.scan() == []
        # Backend was never called because iid was missing.
        assert host.approval_calls == []

    def test_backend_failure_is_silently_swallowed(self) -> None:
        """An exception from ``get_mr_approvals`` is logged but does not crash the tick."""

        class _ExplodingHost(FakeCodeHost):
            def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
                _ = (repo, pr_iid)
                msg = "boom"
                raise RuntimeError(msg)

        host = _ExplodingHost(my_prs=[_gitlab_mr(iid=51, sha="explode")])
        scanner = GitLabApprovalsScanner(host=host)

        # No exception out, no signal in — the per-PR error is contained.
        assert scanner.scan() == []

    def test_duplicate_urls_are_deduped(self) -> None:
        """A PR returned for two aliases (same URL) results in one signal."""
        host = FakeCodeHost(
            my_prs=[
                _gitlab_mr(iid=60, sha="dupe"),
                _gitlab_mr(iid=60, sha="dupe"),  # same URL → deduped
            ],
            approvals={
                ("acme/backend", 60): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host)

        signals = scanner.scan()

        assert len(signals) == 1
        # The dedup happens at the URL level, so the backend is called once.
        assert host.approval_calls == [("acme/backend", 60)]

    def test_identities_override_current_user(self) -> None:
        """Multi-alias identities replace the ``current_user`` fallback (#976 shape)."""
        host = FakeCodeHost(
            user="should-not-be-used",
            my_prs=[_gitlab_mr(iid=50, sha="eee555")],
            approvals={
                ("acme/backend", 50): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        scanner = GitLabApprovalsScanner(host=host, identities=("alice", "alice-org"))

        signals = scanner.scan()

        assert len(signals) == 1


class TestPerPrIsolation(TestCase):
    """Regression tests for per-PR exception isolation (#1592)."""

    def test_sibling_pr_still_emits_when_first_raises_value_error(self) -> None:
        """overlay.can_auto_merge raising for the first PR must not suppress the second.

        Before the fix, the ValueError propagated out of scan(), returning zero
        signals. After the fix, the first PR failure is logged and skipped; the
        second PR emits its signal normally.
        """
        host = FakeCodeHost(
            my_prs=[
                _gitlab_mr(iid=201, sha="bad-mr", project="acme/backend"),
                _gitlab_mr(iid=202, sha="good-mr", project="acme/backend"),
            ],
            approvals={
                ("acme/backend", 201): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
                ("acme/backend", 202): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )

        call_count = 0

        class _RaisingFirstOverlay:
            """Raises ValueError for the first MR, allows the second."""

            def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    msg = "description does not match canonical format"
                    raise ValueError(msg)
                return MergeGuard(allowed=True, reason="", escalate=False)

        scanner = GitLabApprovalsScanner(host=host)
        with patch.object(overlay_loader_mod, "get_overlay", return_value=_RaisingFirstOverlay()):
            signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].kind == "incoming_event.merge_needed"
        assert "merge_requests/202" in signals[0].payload["thread_ref"]

    def test_scanner_error_from_scan_one_propagates(self) -> None:
        """A ScannerError (auth/network) raised by _scan_one must escape scan().

        ScannerError is the structured escalation path — the dispatcher must
        see it to DM the user. The per-PR isolation must re-raise it.
        """

        class _ScannerErrorHost(FakeCodeHost):
            def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
                raise ScannerError(
                    scanner="gitlab_approvals",
                    error_class=ScannerErrorClass.AUTH,
                    detail="401 Unauthorized",
                )

        host = _ScannerErrorHost(
            my_prs=[_gitlab_mr(iid=203, sha="auth-fail")],
        )
        scanner = GitLabApprovalsScanner(host=host)

        with pytest.raises(ScannerError):
            scanner.scan()


class _MergeGuardOverlay(OverlayBase):
    """Concrete overlay owning a repo and returning a fixed merge guard."""

    def __init__(self, *, repos: list[str], guard: MergeGuard) -> None:
        self._repos = repos
        self._guard = guard

    def get_repos(self) -> list[str]:
        return self._repos

    def get_workspace_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        _ = (target_ref, thread_ref)
        return self._guard


class TestGitLabApprovalsMultiOverlay(TestCase):
    """Real ``get_overlay()`` ambiguity path — two overlays registered (TODO-282).

    The scanner held the approved MR's ``url`` but called bare
    ``_overlay_loader.get_overlay()`` to reach ``can_auto_merge``. With two
    overlays registered and no ``T3_OVERLAY_NAME``, that raises
    ``ImproperlyConfigured("Multiple overlays found ...")`` — swallowed by the
    per-PR ``except Exception`` in ``scan()``, so the approved MR emits NOTHING
    and the merge is silently dropped. The fix resolves the overlay from the MR
    URL (``get_overlay_for_url``), so the URL-owning overlay's merge policy runs.

    Nothing about overlay resolution is mocked; only the code host (a network
    external) is. The owning overlay returns an ESCALATE guard so the emitted
    signal proves *that specific overlay's* policy ran, not a permissive default.
    """

    def test_resolves_url_owning_overlay_with_two_registered(self) -> None:
        url = "https://gitlab.com/acme/backend/-/merge_requests/77"
        host = FakeCodeHost(
            my_prs=[_gitlab_mr(iid=77, sha="multi-1", project="acme/backend")],
            approvals={
                ("acme/backend", 77): ApprovalState(
                    approvals_left=0,
                    approved_by=["bob"],
                    unresolved_resolvable=0,
                ),
            },
        )
        owner = _MergeGuardOverlay(
            repos=["acme/backend"],
            guard=MergeGuard(allowed=False, reason="freeze window", escalate=True),
        )
        other = _MergeGuardOverlay(
            repos=["other/repo"],
            guard=MergeGuard(allowed=True),
        )
        overlays = {"acme": owner, "other": other}
        env_without_pin = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        with (
            patch.dict(os.environ, env_without_pin, clear=True),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays),
        ):
            signals = GitLabApprovalsScanner(host=host).scan()

        assert len(signals) == 1, (
            "with two overlays registered, the approved MR must still emit a signal — "
            "a bare get_overlay() raises Multiple-overlays and the MR is silently dropped"
        )
        assert signals[0].kind == "incoming_event.merge_escalation"
        assert signals[0].payload["reason"] == "freeze window"
        assert signals[0].payload["thread_ref"] == url


class TestHelperFunctions(TestCase):
    """Unit-tests for the private helpers — defensive branches the protocol tests don't reach."""

    def test_already_emitted_at_empty_url_returns_false(self) -> None:
        from teatree.loop.scanners.gitlab_approvals import _already_emitted_at  # noqa: PLC0415

        assert _already_emitted_at("", "abc") is False

    def test_already_emitted_at_no_ticket_returns_false(self) -> None:
        from teatree.loop.scanners.gitlab_approvals import _already_emitted_at  # noqa: PLC0415

        # No ticket for this URL exists.
        assert _already_emitted_at("https://gitlab.com/acme/backend/-/merge_requests/9999", "x") is False

    def test_already_emitted_at_swallows_db_error(self) -> None:
        from teatree.loop.scanners import gitlab_approvals as mod  # noqa: PLC0415

        class _ExplodingModel:
            class objects:  # noqa: N801
                @staticmethod
                def filter(*_args: object, **_kwargs: object) -> Any:
                    msg = "DB down"
                    raise RuntimeError(msg)

        with patch.object(mod, "_ticket_model", return_value=_ExplodingModel):
            assert mod._already_emitted_at("https://gitlab.com/a/b/-/merge_requests/1", "x") is False

    def test_record_emission_empty_url_is_noop(self) -> None:
        from teatree.loop.scanners.gitlab_approvals import _record_emission  # noqa: PLC0415

        # Nothing to assert positively — just confirm no exception.
        _record_emission("", "abc")

    def test_record_emission_swallows_db_error(self) -> None:
        from teatree.loop.scanners import gitlab_approvals as mod  # noqa: PLC0415

        class _ExplodingModel:
            class objects:  # noqa: N801
                @staticmethod
                def get_or_create(**_kwargs: object) -> Any:
                    msg = "DB down"
                    raise RuntimeError(msg)

        with patch.object(mod, "_ticket_model", return_value=_ExplodingModel):
            # Must not raise.
            mod._record_emission("https://gitlab.com/a/b/-/merge_requests/1", "abc")

    def test_ticket_model_returns_none_when_apps_unavailable(self) -> None:
        from teatree.loop.scanners import gitlab_approvals as mod  # noqa: PLC0415

        with patch("django.apps.apps.get_model", side_effect=RuntimeError("not ready")):
            assert mod._ticket_model() is None

    def test_already_emitted_at_when_ticket_model_is_none(self) -> None:
        from teatree.loop.scanners import gitlab_approvals as mod  # noqa: PLC0415

        with patch.object(mod, "_ticket_model", return_value=None):
            assert mod._already_emitted_at("https://gitlab.com/a/b/-/merge_requests/1", "x") is False

    def test_record_emission_when_ticket_model_is_none(self) -> None:
        from teatree.loop.scanners import gitlab_approvals as mod  # noqa: PLC0415

        with patch.object(mod, "_ticket_model", return_value=None):
            mod._record_emission("https://gitlab.com/a/b/-/merge_requests/1", "abc")

    def test_int_field_skips_bool_values(self) -> None:
        from teatree.loop.scanners.gitlab_approvals import _int_field  # noqa: PLC0415

        # ``True`` is technically ``int`` in Python — the helper must reject it
        # so a ``"draft": True`` lookup doesn't accidentally return 1.
        assert _int_field({"draft": True}, "draft") == 0
        assert _int_field({"iid": 5}, "iid") == 5


# Re-export the protocol type as a smoke-test that the import path stays stable
# (the dispatcher and downstream consumers reference ``ApprovalState`` by name).
def test_approval_state_typed_dict_shape() -> None:
    state: ApprovalState = {
        "approvals_left": 0,
        "approved_by": ["alice"],
        "unresolved_resolvable": 0,
    }
    assert state["approvals_left"] == 0
    assert state["approved_by"] == ["alice"]
    assert state["unresolved_resolvable"] == 0
