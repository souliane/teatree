"""DB-backed tests for ``TicketDispositionScanner``."""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.ticket_dispositions import TicketDispositionScanner
from teatree.types import RawAPIDict


@dataclass
class _Host:
    """CodeHostBackend stub keyed on ``issue_url`` for ``get_issue`` lookups."""

    user: str = "alice"
    issues_by_url: dict[str, RawAPIDict] = field(default_factory=dict)
    get_issue_calls: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return []

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return []

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
        self.get_issue_calls.append(issue_url)
        return self.issues_by_url.get(issue_url, {"error": "not found"})


class TicketDispositionScannerTests(TestCase):
    OVERLAY = "acme"
    URL = "https://example.com/issues/200"

    def _scanner(self, host: _Host, *, ready_labels: tuple[str, ...] = ("ready",)) -> TicketDispositionScanner:
        return TicketDispositionScanner(host=host, ready_labels=ready_labels, overlay_name=self.OVERLAY)

    def _open_ready_issue(self) -> RawAPIDict:
        return {
            "state": "opened",
            "assignees": [{"username": "alice"}],
            "labels": [{"name": "ready"}],
        }

    def _ticket(self, *, state: str = Ticket.State.STARTED, url: str | None = None) -> Ticket:
        return Ticket.objects.create(overlay=self.OVERLAY, issue_url=url or self.URL, state=state)

    def test_no_signals_when_issue_unchanged(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: self._open_ready_issue()})
        assert self._scanner(host).scan() == []

    def test_flags_closed_issue(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "state": "closed"}})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["issue_closed"]
        assert signals[0].payload["issue_url"] == self.URL

    def test_flags_unassigned_issue(self) -> None:
        self._ticket()
        host = _Host(
            issues_by_url={self.URL: {**self._open_ready_issue(), "assignees": [{"username": "bob"}]}},
        )
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["unassigned"]

    def test_unassigned_signal_carries_old_and_new_owners(self) -> None:
        """The renderer needs both sides to show ``from <old> → to <new>``."""
        self._ticket()
        host = _Host(
            issues_by_url={
                self.URL: {**self._open_ready_issue(), "assignees": [{"username": "bob"}, {"username": "carol"}]},
            },
        )
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["old_owner"] == "alice"  # _Host.current_user()
        assert signal.payload["new_owners"] == ["bob", "carol"]

    def test_non_unassigned_reasons_carry_no_owner_fields(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "labels": [{"name": "blocked"}]}})
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "label_removed")
        assert "old_owner" not in signal.payload
        assert "new_owners" not in signal.payload

    def test_flags_label_removed(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "labels": [{"name": "blocked"}]}})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["label_removed"]

    def test_emits_one_signal_per_reason_when_multiple_apply(self) -> None:
        self._ticket()
        host = _Host(
            issues_by_url={
                self.URL: {
                    "state": "closed",
                    "assignees": [{"username": "bob"}],
                    "labels": [{"name": "blocked"}],
                }
            },
        )
        signals = self._scanner(host).scan()
        reasons = sorted(s.payload["reason"] for s in signals)
        assert reasons == ["issue_closed", "label_removed", "unassigned"]

    def test_skips_tickets_in_post_pr_states(self) -> None:
        for state in (Ticket.State.SHIPPED, Ticket.State.IN_REVIEW, Ticket.State.MERGED, Ticket.State.DELIVERED):
            Ticket.objects.create(overlay=self.OVERLAY, issue_url=f"{self.URL}/{state}", state=state)
        host = _Host()  # never queried — tickets filtered out before host call
        assert self._scanner(host).scan() == []
        assert host.get_issue_calls == []

    def test_skips_tickets_with_empty_url(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url="", state=Ticket.State.STARTED)
        host = _Host()
        assert self._scanner(host).scan() == []
        assert host.get_issue_calls == []

    def test_handles_issue_lookup_error_silently(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={})  # get_issue returns error
        assert self._scanner(host).scan() == []

    def test_does_not_flag_unassigned_when_assignees_empty(self) -> None:
        """An empty assignees list means 'no one' — different from 'reassigned'."""
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "assignees": []}})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == []

    def test_skips_label_check_when_ready_labels_empty(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "labels": []}})
        signals = self._scanner(host, ready_labels=()).scan()
        assert signals == []

    def test_filters_by_overlay_name(self) -> None:
        Ticket.objects.create(overlay="other", issue_url=self.URL, state=Ticket.State.STARTED)
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "state": "closed"}})
        assert self._scanner(host).scan() == []

    def test_supports_github_login_field(self) -> None:
        """GitHub uses 'login', GitLab uses 'username' — both should work."""
        self._ticket()
        host = _Host(
            issues_by_url={self.URL: {**self._open_ready_issue(), "assignees": [{"login": "alice"}]}},
        )
        assert self._scanner(host).scan() == []  # alice IS assigned, no flag


class TicketDispositionScannerAliasTests(TestCase):
    """Suppress the ``unassigned`` (reassign) signal when both sides are the same human.

    The operator has multiple identities across platforms (GitHub login,
    GitLab username, internal handle). A reassign between two of those
    aliases is plumbing noise — the human did not actually hand the work
    off. Reassigns crossing the alias boundary (alias → colleague or
    colleague → alias) still render normally.
    """

    OVERLAY = "acme"
    URL = "https://example.com/issues/300"
    ALIASES: tuple[str, ...] = ("adrien.work", "souliane", "acme.work")

    def _scanner(
        self,
        host: _Host,
        *,
        aliases: tuple[str, ...] = ALIASES,
    ) -> TicketDispositionScanner:
        return TicketDispositionScanner(
            host=host,
            ready_labels=("ready",),
            overlay_name=self.OVERLAY,
            user_identity_aliases=aliases,
        )

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.URL, state=Ticket.State.STARTED)

    def _issue(self, *, assignees: list[dict[str, str]]) -> RawAPIDict:
        return {"state": "opened", "assignees": assignees, "labels": [{"name": "ready"}]}

    def test_alias_to_alias_reassign_is_suppressed(self) -> None:
        """old=adrien.work, new=[souliane] with both in aliases → no signal."""
        self._ticket()
        host = _Host(user="adrien.work", issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}])})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals if s.payload["reason"] == "unassigned"] == []

    def test_alias_to_multiple_aliases_reassign_is_suppressed(self) -> None:
        """old=adrien.work, new=[souliane, acme.work] all in aliases → no signal."""
        self._ticket()
        host = _Host(
            user="adrien.work",
            issues_by_url={
                self.URL: self._issue(assignees=[{"username": "souliane"}, {"username": "acme.work"}]),
            },
        )
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals if s.payload["reason"] == "unassigned"] == []

    def test_alias_to_colleague_still_renders(self) -> None:
        """old=adrien.work (alias), new=[some-colleague] (not alias) → signal kept."""
        self._ticket()
        host = _Host(user="adrien.work", issues_by_url={self.URL: self._issue(assignees=[{"username": "colleague"}])})
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["old_owner"] == "adrien.work"
        assert signal.payload["new_owners"] == ["colleague"]

    def test_alias_to_mixed_alias_and_colleague_still_renders(self) -> None:
        """New contains one alias and one colleague → not fully within the alias set → keep the signal."""
        self._ticket()
        host = _Host(
            user="adrien.work",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}, {"username": "colleague"}])},
        )
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["new_owners"] == ["souliane", "colleague"]

    def test_non_alias_to_alias_still_renders(self) -> None:
        """A colleague handed work back to the operator — still actionable."""
        self._ticket()
        host = _Host(user="colleague", issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}])})
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["old_owner"] == "colleague"
        assert signal.payload["new_owners"] == ["souliane"]

    def test_empty_aliases_default_keeps_legacy_behaviour(self) -> None:
        """With no aliases configured (default), every reassign still renders."""
        self._ticket()
        host = _Host(user="adrien.work", issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}])})
        signals = self._scanner(host, aliases=()).scan()
        assert [s.payload["reason"] for s in signals if s.payload["reason"] == "unassigned"] == ["unassigned"]

    def test_suppression_does_not_block_other_reasons(self) -> None:
        """A self-handoff that ALSO has issue_closed must still emit issue_closed."""
        self._ticket()
        host = _Host(
            user="adrien.work",
            issues_by_url={
                self.URL: {
                    "state": "closed",
                    "assignees": [{"username": "souliane"}],
                    "labels": [{"name": "ready"}],
                },
            },
        )
        reasons = sorted(s.payload["reason"] for s in self._scanner(host).scan())
        assert reasons == ["issue_closed"]


class _FakeBackend:
    """Minimal :class:`OverlayBackends` stand-in for ``_jobs_for_backend_hosts``.

    Carries the trusted multi-identity operator set on ``identities`` but
    leaves ``overlay`` ``None`` so no explicit ``identity_aliases`` config
    is contributed — exactly the user's deployment shape (#1113 Defect 1):
    aliases empty everywhere, only the operator identity set known.
    """

    def __init__(self, host: _Host, *, name: str, identities: tuple[str, ...]) -> None:
        self.name = name
        self.hosts = (host,)
        self.messaging = None
        self.ready_labels: tuple[str, ...] = ("ready",)
        self.exclude_labels: tuple[str, ...] = ()
        self.overlay = None
        self.auto_start_assigned_issues = False
        self.max_concurrent_auto_starts = 1
        self.stale_threshold_days = 3
        self.external_db = None
        self.identities = identities


class TicketDispositionBackendIdentitySelfGroupTests(TestCase):
    """#1113 Defect 1 — the trusted operator identity set is an implicit self-group.

    ``_user_identity_aliases_for_overlay`` / ``_identity_alias_groups_for_overlay``
    both resolve empty in the user's deployment (no explicit config), so the
    scanner's self-handoff predicate never fired and same-human reassigns
    (``acme-gh → souliane``) rendered as ``reassigned`` churn. The fix
    unions ``backend.identities`` into the alias groups passed to the
    disposition scanner. This drives the real
    ``scanner_factories._jobs_for_backend_hosts`` builder → scan → dispatch →
    render pipeline (integration, not a hand-rolled comparator).
    """

    OVERLAY = "t3-teatree"
    URL = "https://github.com/souliane/teatree/issues/900"
    IDENTITIES: tuple[str, ...] = ("acme-gh", "souliane", "acme.work")

    def _disposition_scanner(self, backend: _FakeBackend) -> TicketDispositionScanner:
        from teatree.loop.scanner_factories import _jobs_for_backend_hosts  # noqa: PLC0415

        jobs = _jobs_for_backend_hosts(backend, self.OVERLAY)
        scanner = next(j.scanner for j in jobs if j.scanner.name == "ticket_dispositions")
        assert isinstance(scanner, TicketDispositionScanner)
        return scanner

    def _render_blob(self, scanner: TicketDispositionScanner) -> str:
        from teatree.loop.dispatch import dispatch  # noqa: PLC0415
        from teatree.loop.rendering import zones_for  # noqa: PLC0415

        signals = [
            ScanSignal(kind=s.kind, summary=s.summary, payload={**s.payload, "overlay": self.OVERLAY})
            for s in scanner.scan()
        ]
        zones = zones_for(dispatch(signals), colorize=False)
        return "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )

    def test_reassign_between_operator_identities_emits_no_reassigned_row(self) -> None:
        """acme-gh → souliane (both in backend.identities) → no reassigned churn."""
        Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.URL, state=Ticket.State.STARTED)
        host = _Host(
            user="acme-gh",
            issues_by_url={
                self.URL: {
                    "state": "opened",
                    "assignees": [{"login": "acme.work"}],
                    "labels": [{"name": "ready"}],
                },
            },
        )
        backend = _FakeBackend(host, name=self.OVERLAY, identities=self.IDENTITIES)
        blob = self._render_blob(self._disposition_scanner(backend))
        assert "reassigned (from acme-gh → to" not in blob, repr(blob)

    def test_reassign_to_real_colleague_still_renders(self) -> None:
        """acme-gh → colleague (∉ backend.identities) — still an actionable handoff."""
        Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.URL, state=Ticket.State.STARTED)
        host = _Host(
            user="acme-gh",
            issues_by_url={
                self.URL: {
                    "state": "opened",
                    "assignees": [{"login": "real-colleague"}],
                    "labels": [{"name": "ready"}],
                },
            },
        )
        backend = _FakeBackend(host, name=self.OVERLAY, identities=self.IDENTITIES)
        blob = self._render_blob(self._disposition_scanner(backend))
        assert "reassigned (from acme-gh → to real-colleague):" in blob, repr(blob)

    def test_single_identity_backend_does_not_trigger_self_group_union(self) -> None:
        """Explicit identity-group config must take precedence over backend.identities.

        A single-identity ``backend.identities`` (no real multi-handle
        ambiguity) must NOT short-circuit reassigns to itself — the union
        path is skipped when an explicit group is already set.
        """
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.scanner_factories import _jobs_for_backend_hosts  # noqa: PLC0415

        # An explicit non-empty groups tuple skips the union branch entirely.
        backend = _FakeBackend(_Host(user="acme-gh"), name=self.OVERLAY, identities=("acme-gh",))
        with patch(
            "teatree.loop.scanner_factories._identity_alias_groups_for_overlay",
            return_value=(("explicit", "group"),),
        ):
            jobs = _jobs_for_backend_hosts(backend, self.OVERLAY)
        disposition = next(j.scanner for j in jobs if j.scanner.name == "ticket_dispositions")
        assert isinstance(disposition, TicketDispositionScanner)
        assert disposition.identity_alias_groups == (("explicit", "group"),)
