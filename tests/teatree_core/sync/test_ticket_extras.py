"""Ticket update and extra-field merge tests (souliane/teatree#443 split of test_sync.py).

Covers update_ticket field preservation and merge_ticket_extras.
"""

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.backends.gitlab_sync_prs import _PRContext, merge_ticket_extras, update_ticket, upsert_ticket_from_pr
from teatree.core.e2e_workitem import record_run
from teatree.core.gates import dod_gate
from teatree.core.models import Ticket
from teatree.types import SyncResult

if TYPE_CHECKING:
    from teatree.types import PREntryDict


def _patch_dod_overlay(frontend_repos: list[str]) -> AbstractContextManager[MagicMock]:
    """Patch the frontend-repo resolution seam the DoD gate delegates to (UI-visibility in sync tests)."""
    return patch.object(dod_gate, "frontend_repos_for_overlay", return_value=list(frontend_repos))


class TestUpdateTicket(TestCase):
    def test_preserves_skill_written_fields(self) -> None:
        """Skill-written fields (review_channel, review_permalink, e2e_test_plan_url) survive sync updates."""
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/200",
            repos=["repo"],
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/50": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/50",
                        "repo": "repo",
                        "title": "feat: old title",
                        "review_channel": "#backend-review",
                        "review_permalink": "https://slack.com/archives/C123/p456",
                        "e2e_test_plan_url": "https://gitlab.com/org/repo/-/merge_requests/50#note_789",
                    },
                },
            },
        )

        # Simulate a sync update that doesn't include the skill-written fields
        new_mr_entry: PREntryDict = {
            "url": "https://gitlab.com/org/repo/-/merge_requests/50",
            "repo": "repo",
            "title": "feat: new title",
            "pipeline_status": "success",
        }

        mr_url = "https://gitlab.com/org/repo/-/merge_requests/50"
        update_ticket(ticket, new_mr_entry, mr_url, "repo")

        ticket.refresh_from_db()
        mr = ticket.extra["prs"]["https://gitlab.com/org/repo/-/merge_requests/50"]
        assert mr["title"] == "feat: new title"
        assert mr["review_channel"] == "#backend-review"
        assert mr["review_permalink"] == "https://slack.com/archives/C123/p456"
        assert mr["e2e_test_plan_url"] == "https://gitlab.com/org/repo/-/merge_requests/50#note_789"

    def test_does_not_clobber_a_concurrent_writers_extra_key(self) -> None:
        """A concurrent writer's top-level extra key survives a sync from a stale ticket.

        update_ticket only mutates the top-level ``prs`` key, so it must
        pass ``set_keys={"prs": ...}`` to ``merge_extra`` -- not the whole
        stale ``extra`` snapshot. Passing the whole snapshot makes
        ``merge_extra``'s locked re-read overwrite every sibling key
        (reviewed_sha, last_approval_sha, pr_urls, visual_qa) with the
        stale value, defeating the lock (the #800 lost-update class).

        Modelled as the canonical lost-update race: a stale in-memory
        ticket read first, then the reviewer path commits ``reviewed_sha``
        via the same locked primitive (bare autocommit, the prod shape),
        then the sync runs from the stale handle.
        """
        mr_url = "https://gitlab.com/org/repo/-/merge_requests/50"
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/210",
            repos=["repo"],
            extra={"reviewed_sha": "old_sha", "prs": {mr_url: {"url": mr_url, "repo": "repo", "title": "old"}}},
        )

        # Stale handle read BEFORE the concurrent writer commits.
        stale = Ticket.objects.get(pk=ticket.pk)

        # Concurrent reviewer-path writer stamps a fresh reviewed_sha.
        ticket.merge_extra(set_keys={"reviewed_sha": "new_sha"})

        # The sync runs from the stale in-memory ticket.
        new_mr_entry: PREntryDict = {"url": mr_url, "repo": "repo", "title": "new title"}
        update_ticket(stale, new_mr_entry, mr_url, "repo")

        stale.refresh_from_db()
        # The concurrent writer's key must survive (the lock did its job).
        assert stale.extra["reviewed_sha"] == "new_sha"
        # The sync's own mutation still landed.
        assert stale.extra["prs"][mr_url]["title"] == "new title"


class TestMergeTicketExtras(TestCase):
    def test_combines_mrs_and_repos(self) -> None:
        """_merge_ticket_extras merges MR entries and repos from source into target."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/900",
            repos=["repo-a"],
            extra={"prs": {"https://mr/1": {"title": "MR 1"}}},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/901",
            repos=["repo-b"],
            extra={"prs": {"https://mr/2": {"title": "MR 2"}}},
        )
        merge_ticket_extras(target, source)
        target.refresh_from_db()

        assert "https://mr/1" in target.extra["prs"]
        assert "https://mr/2" in target.extra["prs"]
        assert "repo-a" in target.repos
        assert "repo-b" in target.repos

    def test_handles_non_dict_mrs(self) -> None:
        """Non-dict prs in extras are treated as empty -- repos still merge."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/960",
            repos=["repo-a"],
            extra={"prs": "corrupt"},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/961",
            repos=["repo-b"],
            extra={"prs": ["also-corrupt"]},
        )
        merge_ticket_extras(target, source)
        target.refresh_from_db()
        assert target.repos == ["repo-a", "repo-b"]

    def test_skips_overlapping_mrs_and_repos(self) -> None:
        """Overlapping MR URLs and repos are not duplicated."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/950",
            repos=["repo-a", "repo-b"],
            extra={"prs": {"https://mr/1": {"title": "MR 1"}}},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/951",
            repos=["repo-b", "repo-c"],
            extra={"prs": {"https://mr/1": {"title": "MR 1 dup"}, "https://mr/3": {"title": "MR 3"}}},
        )
        merge_ticket_extras(target, source)
        target.refresh_from_db()

        assert target.extra["prs"]["https://mr/1"]["title"] == "MR 1"
        assert "https://mr/3" in target.extra["prs"]
        assert target.repos == ["repo-a", "repo-b", "repo-c"]


_FRONTEND_REPO = "frontend"
_BACKEND_REPO = "backend"


def _non_draft_pr_ctx(repo: str, *, iid: int = 60) -> _PRContext:
    """A _PRContext whose raw MR is non-draft (so it infers SHIPPED)."""
    web_url = f"https://gitlab.com/org/repo/-/merge_requests/{iid}"
    raw = {
        "web_url": web_url,
        "title": "feat: visible change",
        "description": "feat: visible change",
        "source_branch": "feat/visible",
        "draft": False,
        "iid": iid,
    }
    # project=None keeps build_pr_entry on the cheap path (no pipeline/approval
    # fetches) so the entry is a plain non-draft PR with no approvals: SHIPPED.
    return _PRContext(raw=raw, repo_short=repo, client=MagicMock(), project=None)


class TestSyncRespectsDodGate(TestCase):
    """The PR-sync path must not bypass the #88 DoD gate to write SHIPPED.

    ``infer_state_from_prs`` returns SHIPPED for a non-draft PR with no
    approvals. Both sync write paths (``upsert_ticket_from_pr`` create and
    ``update_ticket``) wrote that state DIRECTLY, never through ``ship()``,
    so the DoD local-E2E gate never fired. A UI-visible ticket with no green
    local-stack E2E must NOT reach SHIPPED via sync.
    """

    def test_create_does_not_ship_ui_visible_ticket_without_local_e2e(self) -> None:
        ctx = _non_draft_pr_ctx(_FRONTEND_REPO, iid=61)
        result = SyncResult()
        with _patch_dod_overlay([_FRONTEND_REPO]):
            upsert_ticket_from_pr(ctx, result, overlay_name="acme")

        ticket = Ticket.objects.get(issue_url=ctx.raw["web_url"])
        assert ticket.state != Ticket.State.SHIPPED
        assert ticket.state == Ticket.State.STARTED

    def test_create_ships_ui_visible_ticket_with_green_local_e2e(self) -> None:
        ctx = _non_draft_pr_ctx(_FRONTEND_REPO, iid=62)
        result = SyncResult()
        with (
            _patch_dod_overlay([_FRONTEND_REPO]),
            patch.object(dod_gate, "has_local_e2e_artifact", return_value=True),
        ):
            upsert_ticket_from_pr(ctx, result, overlay_name="acme")

        ticket = Ticket.objects.get(issue_url=ctx.raw["web_url"])
        assert ticket.state == Ticket.State.SHIPPED

    def test_create_ships_backend_only_ticket(self) -> None:
        ctx = _non_draft_pr_ctx(_BACKEND_REPO, iid=63)
        result = SyncResult()
        with _patch_dod_overlay([_FRONTEND_REPO]):
            upsert_ticket_from_pr(ctx, result, overlay_name="acme")

        ticket = Ticket.objects.get(issue_url=ctx.raw["web_url"])
        assert ticket.state == Ticket.State.SHIPPED

    def test_create_ships_ui_visible_ticket_with_override(self) -> None:
        ctx = _non_draft_pr_ctx(_FRONTEND_REPO, iid=64)
        result = SyncResult()
        with (
            _patch_dod_overlay([_FRONTEND_REPO]),
            patch.object(
                dod_gate,
                "override_reason",
                return_value="exempt: backend-only despite repo set",
            ),
        ):
            upsert_ticket_from_pr(ctx, result, overlay_name="acme")

        ticket = Ticket.objects.get(issue_url=ctx.raw["web_url"])
        assert ticket.state == Ticket.State.SHIPPED

    def test_update_does_not_ship_ui_visible_ticket_without_local_e2e(self) -> None:
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/65"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/365",
            repos=[_FRONTEND_REPO],
            state=Ticket.State.STARTED,
        )
        pr_entry: PREntryDict = {"url": pr_url, "repo": _FRONTEND_REPO, "title": "feat: visible", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _FRONTEND_REPO, Ticket.State.SHIPPED)

        ticket.refresh_from_db()
        assert ticket.state != Ticket.State.SHIPPED
        assert ticket.state == Ticket.State.STARTED

    def test_update_ships_ui_visible_ticket_with_green_local_e2e(self) -> None:
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/66"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/366",
            repos=[_FRONTEND_REPO],
            state=Ticket.State.STARTED,
        )
        record_run(ticket, result="green", per_repo_shas={_FRONTEND_REPO: "sha"}, env="local")
        pr_entry: PREntryDict = {"url": pr_url, "repo": _FRONTEND_REPO, "title": "feat: visible", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _FRONTEND_REPO, Ticket.State.SHIPPED)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_update_does_not_advance_ui_visible_no_e2e_to_in_review(self) -> None:
        """IN_REVIEW is past SHIPPED on the FSM, so it is gated too.

        IN_REVIEW is reached only via ``ship() -> request_review()``, so a
        UI-visible no-E2E ticket must not be synced there either.
        """
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/67"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/367",
            repos=[_FRONTEND_REPO],
            state=Ticket.State.STARTED,
        )
        pr_entry: PREntryDict = {"url": pr_url, "repo": _FRONTEND_REPO, "title": "feat: visible", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _FRONTEND_REPO, Ticket.State.IN_REVIEW)

        ticket.refresh_from_db()
        assert ticket.state != Ticket.State.IN_REVIEW
        assert ticket.state == Ticket.State.STARTED

    def test_update_advances_ui_visible_with_green_e2e_to_in_review(self) -> None:
        """A green local E2E satisfies the gate, so IN_REVIEW sync applies."""
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/70"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/370",
            repos=[_FRONTEND_REPO],
            state=Ticket.State.STARTED,
        )
        record_run(ticket, result="green", per_repo_shas={_FRONTEND_REPO: "sha"}, env="local")
        pr_entry: PREntryDict = {"url": pr_url, "repo": _FRONTEND_REPO, "title": "feat: visible", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _FRONTEND_REPO, Ticket.State.IN_REVIEW)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_update_advances_backend_only_to_in_review(self) -> None:
        """A backend-only (not UI-visible) ticket syncs to IN_REVIEW unimpeded."""
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/71"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/371",
            repos=[_BACKEND_REPO],
            state=Ticket.State.STARTED,
        )
        pr_entry: PREntryDict = {"url": pr_url, "repo": _BACKEND_REPO, "title": "fix: backend", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _BACKEND_REPO, Ticket.State.IN_REVIEW)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_update_does_not_downgrade_in_review_to_started(self) -> None:
        """Capping a blocked SHIPPED must not drag a higher current state down."""
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/68"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/368",
            repos=[_FRONTEND_REPO],
            state=Ticket.State.IN_REVIEW,
        )
        pr_entry: PREntryDict = {"url": pr_url, "repo": _FRONTEND_REPO, "title": "feat: visible", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _FRONTEND_REPO, Ticket.State.SHIPPED)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_update_gates_when_sync_first_adds_the_frontend_repo(self) -> None:
        """A frontend repo newly scoped by this very sync still triggers the gate.

        The gate's UI-visibility check reads ``ticket.repos``; the synced
        repo must be reflected in-memory BEFORE the check, or a sync that is
        the first to scope the frontend repo would slip a SHIPPED write past
        the gate on the stale (backend-only) repo set.
        """
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/69"
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/369",
            repos=[_BACKEND_REPO],
            state=Ticket.State.STARTED,
        )
        pr_entry: PREntryDict = {"url": pr_url, "repo": _FRONTEND_REPO, "title": "feat: visible", "draft": False}
        with _patch_dod_overlay([_FRONTEND_REPO]):
            update_ticket(ticket, pr_entry, pr_url, _FRONTEND_REPO, Ticket.State.SHIPPED)

        ticket.refresh_from_db()
        assert _FRONTEND_REPO in ticket.repos
        assert ticket.state != Ticket.State.SHIPPED
        assert ticket.state == Ticket.State.STARTED
