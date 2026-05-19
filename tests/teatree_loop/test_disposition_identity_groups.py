"""Identity-group dedup on the ticket-disposition scanner (#1015).

The legacy ``user_identity_aliases`` is a flat list — it conflates all
configured handles into one set, which works when only one human is on the
overlay. ``identity_alias_groups`` is the explicit grouped shape: each
inner tuple is one human's aliases. A reassignment is suppressed when some
group contains both ``old_owner`` and every ``new_owner`` — i.e. one human
just swapped between their own handles. Cross-group reassigns (human A's
handle → human B's handle) still surface.

Backwards compatibility: a non-empty ``user_identity_aliases`` is treated
as one group, so existing config stays valid.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.ticket_dispositions import TicketDispositionScanner
from teatree.types import RawAPIDict


@dataclass
class _Host:
    user: str = "acme-gh"
    issues_by_url: dict[str, RawAPIDict] = field(default_factory=dict)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
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
        return self.issues_by_url.get(issue_url, {"error": "not found"})


class TicketDispositionIdentityGroupTests(TestCase):
    OVERLAY = "acme"
    URL = "https://gitlab.com/acme/product/-/issues/12"
    GROUPS: tuple[tuple[str, ...], ...] = (
        ("acme-gh", "souliane", "acme.work"),
        ("alice", "alice.work"),
    )

    def _scanner(
        self,
        host: _Host,
        *,
        groups: tuple[tuple[str, ...], ...] = GROUPS,
        flat_aliases: tuple[str, ...] = (),
    ) -> TicketDispositionScanner:
        return TicketDispositionScanner(
            host=host,
            ready_labels=("ready",),
            overlay_name=self.OVERLAY,
            identity_alias_groups=groups,
            user_identity_aliases=flat_aliases,
        )

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.URL, state=Ticket.State.STARTED)

    def _issue(self, *, assignees: list[dict[str, str]]) -> RawAPIDict:
        return {"state": "opened", "assignees": assignees, "labels": [{"name": "ready"}]}

    def test_intra_group_reassign_is_suppressed(self) -> None:
        """acme-gh -> souliane: same human, no signal."""
        self._ticket()
        host = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}])},
        )
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals if s.payload["reason"] == "unassigned"] == []

    def test_three_way_round_trip_within_group_is_suppressed(self) -> None:
        """acme-gh -> acme.work: still within the same group."""
        self._ticket()
        host = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "acme.work"}])},
        )
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals if s.payload["reason"] == "unassigned"] == []

    def test_cross_group_reassign_is_kept(self) -> None:
        """acme-gh -> alice: different humans, still actionable."""
        self._ticket()
        host = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "alice"}])},
        )
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["old_owner"] == "acme-gh"
        assert signal.payload["new_owners"] == ["alice"]

    def test_reassign_to_outsider_is_kept(self) -> None:
        """acme-gh -> colleague: outsider, surface the handoff."""
        self._ticket()
        host = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "stranger"}])},
        )
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["old_owner"] == "acme-gh"
        assert signal.payload["new_owners"] == ["stranger"]

    def test_partial_group_match_still_renders(self) -> None:
        """New owners straddle the group boundary -> NOT a self-handoff."""
        self._ticket()
        host = _Host(
            user="acme-gh",
            issues_by_url={
                self.URL: self._issue(
                    assignees=[{"username": "souliane"}, {"username": "stranger"}],
                ),
            },
        )
        signal = next(s for s in self._scanner(host).scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["new_owners"] == ["souliane", "stranger"]

    def test_flat_aliases_treated_as_one_group_when_no_groups_set(self) -> None:
        """Legacy flat-list config still suppresses intra-list reassigns."""
        self._ticket()
        host = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}])},
        )
        scanner = TicketDispositionScanner(
            host=host,
            ready_labels=("ready",),
            overlay_name=self.OVERLAY,
            user_identity_aliases=("acme-gh", "souliane"),
        )
        signals = scanner.scan()
        assert [s.payload["reason"] for s in signals if s.payload["reason"] == "unassigned"] == []

    def test_chained_reassignments_only_emit_cross_group_handoff(self) -> None:
        """The brief's scenario: only the cross-group handoff emits.

        ``acme-gh -> souliane`` is intra-group (suppressed); a
        subsequent ``souliane -> colleague`` is cross-group (emitted).
        Two separate scans simulate the chain. The first sees ``author=
        acme-gh`` and ``assignees=[souliane]``; both inside the same
        group, no signal. The second sees ``author=souliane`` (the host's
        current user has changed because that handle now owns the ticket)
        and ``assignees=[stranger]``; cross-group, the signal surfaces.
        """
        self._ticket()

        # First reassignment: acme-gh -> souliane (intra-group)
        host1 = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "souliane"}])},
        )
        first = self._scanner(host1).scan()
        assert [s for s in first if s.payload["reason"] == "unassigned"] == []

        # Second reassignment: souliane -> stranger (cross-group)
        host2 = _Host(
            user="souliane",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "stranger"}])},
        )
        second = self._scanner(host2).scan()
        emit = [s for s in second if s.payload["reason"] == "unassigned"]
        assert len(emit) == 1
        assert emit[0].payload["old_owner"] == "souliane"
        assert emit[0].payload["new_owners"] == ["stranger"]

    def test_groups_override_flat_when_both_are_set(self) -> None:
        """Explicit groups take precedence; flat aliases are ignored if groups present."""
        self._ticket()
        # The flat list would mark `acme-gh -> stranger` as a self-handoff,
        # but the explicit group does not include `stranger`, so the signal
        # must surface.
        host = _Host(
            user="acme-gh",
            issues_by_url={self.URL: self._issue(assignees=[{"username": "stranger"}])},
        )
        scanner = TicketDispositionScanner(
            host=host,
            ready_labels=("ready",),
            overlay_name=self.OVERLAY,
            identity_alias_groups=(("acme-gh", "souliane"),),
            user_identity_aliases=("acme-gh", "souliane", "stranger"),
        )
        signal = next(s for s in scanner.scan() if s.payload["reason"] == "unassigned")
        assert signal.payload["new_owners"] == ["stranger"]
