from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree import visual_qa
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import (
    _assert_commits_ahead_of_base,
    _check_shipping_gate,
    _resolve_base_url,
    _run_visual_qa_gate,
)
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.orphan_guard import BranchReport, BranchStatus
from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


def _shippable_ticket() -> Ticket:
    """Build a ticket pre-advanced to REVIEWED with the shipping gate satisfied."""
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
    session = Session.objects.create(ticket=ticket, overlay="test")
    session.visit_phase("testing")
    session.visit_phase("reviewing")
    session.visit_phase("retro")
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path="/tmp/backend",
        branch="feature-branch",
        extra={"worktree_path": "/tmp/backend"},
    )
    return ticket


class TestPrCreateThinWrapper(TestCase):
    """``pr create`` validates gates then triggers ``ticket.ship()`` (#140)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_advances_to_shipped_when_gates_pass(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        # Default (async) path: queued, with an explicit no-worker warning (#708).
        assert result["ticket_id"] == ticket.pk
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True
        assert "QUEUED, not performed" in result["warning"]
        assert "--sync" in result["warning"]

    def test_title_override_persisted_via_locked_merge_extra(self) -> None:
        """#800 N3: ``--title`` writes pr_title_override via locked merge.

        ``_do_ship_transition``'s ``if title:`` path now routes through
        the canonical locked ``merge_extra`` (was an unlocked
        in-``atomic`` whole-extra overwrite racing the ship worker's
        ``pr_urls``). Exercises that branch.
        """
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            call_command("pr", "create", str(ticket.id), title="My Custom PR Title")

        ticket.refresh_from_db()
        assert ticket.extra["pr_title_override"] == "My Custom PR Title"
        assert ticket.state == Ticket.State.SHIPPED

    def test_retrospected_with_satisfying_phases_reconciles_and_ships(self) -> None:
        """#808: RETROSPECTED with a satisfying phase ledger must ship, not deny.

        A non-terminal state with satisfying aggregated phase records must
        NOT return {'allowed': False, 'missing': []}.

        The recurring enumerated-source bug: #799 added IN_REVIEW; a ticket
        re-provisioned for a new workstream whose FSM lingered at
        RETROSPECTED was still denied with the contradictory empty-missing
        gate failure. Phase-driven reconcile makes any non-terminal state
        with satisfying records ship.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.RETROSPECTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        # The exact recurring contradiction must NOT occur.
        assert result.get("missing") != [] or result.get("allowed") is not False, (
            f"reconcile still enumerated — denied from RETROSPECTED with empty missing: {result}"
        )
        assert ticket.state == Ticket.State.SHIPPED
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True

    def test_in_review_with_phases_split_across_three_sessions_reconciles_and_ships(self) -> None:
        """3-session-split + IN_REVIEW must ship, not deadlock (#798).

        The required chain split across 3 sessions (the maker!=checker +
        fresh-session norm) with the ticket stuck at IN_REVIEW (a failed /
        incomplete prior ship) must NOT return {'allowed': False, 'missing':
        []}. The gate aggregates phases across all sessions (so missing is
        empty); the FSM reconcile must likewise recover IN_REVIEW -> REVIEWED
        so ship() is legal and the ticket ships.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        # Three distinct sessions, one phase each, distinct agent identities —
        # exactly how maker!=checker + fresh-session scheduling scatters the
        # required chain across the ticket lifecycle.
        s_test = Session.objects.create(ticket=ticket, overlay="test", agent_id="maker:a")
        s_test.visit_phase("testing", agent_id="maker:a")
        s_review = Session.objects.create(ticket=ticket, overlay="test", agent_id="reviewer:b")
        s_review.visit_phase("reviewing", agent_id="reviewer:b")
        s_retro = Session.objects.create(ticket=ticket, overlay="test", agent_id="maker:a")
        s_retro.visit_phase("retro", agent_id="maker:a")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        # Must NOT be the {'allowed': False, 'missing': []} deadlock.
        assert result.get("missing") != [] or result.get("allowed") is not False
        assert "error" not in result, result
        assert ticket.state == Ticket.State.SHIPPED
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True

    def test_returns_error_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))
        assert "error" in result

    def test_dry_run_returns_preview_without_transition(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), dry_run=True))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # unchanged
        assert result["dry_run"] is True
        assert result["title"] == "feat: x"
        assert result["branch"] == "feature-branch"

    def test_resolves_ticket_by_issue_url(self) -> None:
        # Calling `pr create` with the issue URL (or trailing issue number)
        # resolves to the ticket by issue_url so users don't have to look up
        # the internal DB pk first.
        ticket = _shippable_ticket()
        ticket.issue_url = "https://github.com/souliane/teatree/issues/466"
        ticket.save(update_fields=["issue_url"])

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", "https://github.com/souliane/teatree/issues/466"),
            )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result["ticket_id"] == ticket.pk

    def test_blocked_when_visual_qa_fails(self) -> None:
        ticket = _shippable_ticket()
        failure = pr_command.VisualQAGateFailure(
            allowed=False,
            error="Visual QA found 1 blocking finding(s).",
            visual_qa={},
            report_markdown="## Visual QA",
            hint="fix it",
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=failure),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # not advanced
        assert result["allowed"] is False


class TestPrCreateRecordsInvokingBranch(TestCase):
    """#776 B1: ``create()`` must PRODUCE ``extra['ship_invoking_branch']``.

    The producer block (capture ``git.current_branch`` → persist the
    hint) is the single highest-value line of #776 — ShipExecutor's
    consumer side is meaningless without it. These tests exercise the
    producer directly via ``call_command``, patching only ``git`` (an
    unstoppable external). ``dry_run=True`` isolates the producer: it
    runs before the dry-run early-return and before the gates, so no
    ship mocking is needed.
    """

    def test_persists_invoking_branch_from_git_current_branch(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="s-776-feature"),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            call_command("pr", "create", str(ticket.id), dry_run=True)

        ticket.refresh_from_db()
        assert ticket.extra["ship_invoking_branch"] == "s-776-feature"

    def test_guard_skips_persistence_on_non_feature_branch(self) -> None:
        # subTest (not @parametrize): this file's classes are
        # django.test.TestCase, which pytest does not parametrize.
        for protected in ("HEAD", "main", "master"):
            with self.subTest(branch=protected):
                ticket = _shippable_ticket()
                with (
                    patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
                    patch.object(pr_command.git, "current_branch", return_value=protected),
                    patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
                ):
                    call_command("pr", "create", str(ticket.id), dry_run=True)

                ticket.refresh_from_db()
                assert "ship_invoking_branch" not in (ticket.extra or {})

    def test_guard_skips_persistence_on_empty_branch(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value=""),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            call_command("pr", "create", str(ticket.id), dry_run=True)

        ticket.refresh_from_db()
        assert "ship_invoking_branch" not in (ticket.extra or {})


class TestPrCreateSyncShip(TestCase):
    """`pr create --sync` runs the ship inline; async warns it is queued (#708)."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_sync_runs_execute_ship_inline(self) -> None:
        ticket = _shippable_ticket()
        ship_mock = MagicMock()
        ship_mock.call.return_value = {"ticket_id": ticket.pk, "ok": True, "detail": "PR opened"}

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )

        ship_mock.call.assert_called_once_with(ticket.pk)
        assert result["synced"] is True
        assert result["ok"] is True
        assert result["detail"] == "PR opened"
        assert result["ticket_id"] == ticket.pk

    def test_sync_reports_ship_failure_without_raising(self) -> None:
        ticket = _shippable_ticket()
        ship_mock = MagicMock()
        ship_mock.call.return_value = {"ticket_id": ticket.pk, "ok": False, "detail": "push rejected"}

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )

        assert result["synced"] is True
        assert result["ok"] is False
        assert result["detail"] == "push rejected"

    def test_async_default_does_not_call_execute_ship_inline(self) -> None:
        ticket = _shippable_ticket()
        ship_mock = MagicMock()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id)),
            )

        ship_mock.call.assert_not_called()
        assert result["queued"] is True
        assert "QUEUED, not performed" in result["warning"]

    def test_skip_validation_reconciles_fsm_then_ships_a_non_reviewed_ticket(self) -> None:
        """#748: ``--skip-validation`` reconciles the FSM then ships.

        ``--skip-validation`` is the user-authorized attestation
        substitute, so the FSM must follow the authorization.
        Pre-fix, ``--skip-validation`` skipped the phase check AND the FSM
        reconcile, so ``ship()`` failed from a non-REVIEWED state — the
        gate-fixer bootstrap exception was structurally broken (it could
        never actually ship the very tickets it exists for). The skip
        path now walks the FSM to REVIEWED via ``reconcile_reviewed`` so
        ``ship()`` is legal. RED on the pre-fix body (returns the
        "Cannot ship from state" gate failure); GREEN once the skip path
        reconciles.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        ship_mock = MagicMock()
        ship_mock.call.return_value = {"ticket_id": ticket.pk, "ok": True, "detail": "PR opened"}
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True, skip_validation=True),
            )

        # The authorized bypass now ships: FSM reconciled to REVIEWED,
        # ship() legal, no "Cannot ship from state" failure.
        assert result.get("allowed") is not False, result
        assert result["ok"] is True
        ticket.refresh_from_db()
        assert ticket.state in {Ticket.State.SHIPPED, Ticket.State.REVIEWED}

    def test_sync_illegal_transition_without_skip_is_structured_failure(self) -> None:
        # Validation NOT skipped, no attested session -> the gate blocks
        # with a structured failure, never a raw TransitionNotAllowed (#694).
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )
        assert result["allowed"] is False
        assert result["error"]

    def _assert_skip_validation_post_ship_no_raw_transition(
        self, post_state: Ticket.State, expected_state: Ticket.State
    ) -> None:
        """``--skip-validation`` from a post-ship state never raises raw (#694/#748).

        It must degrade to a structured dict result rather than raising a
        raw ``TransitionNotAllowed``.

        The resulting FSM state depends on the start state: ``MERGED`` is a
        genuine terminal (no reconcile source) so it stays unchanged;
        ``IN_REVIEW`` is now a recoverable source (#798) so a gate/auth-
        passing ticket reconciles ``IN_REVIEW → REVIEWED`` and re-ships
        (``execute_ship`` is state-guarded/idempotent). The safety
        invariant (no raw raise) holds for both.
        """
        ticket = Ticket.objects.create(overlay="test", state=post_state)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        ship_mock = MagicMock()
        ship_mock.call.return_value = {"ticket_id": ticket.pk, "ok": True, "detail": "PR opened"}
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            # Must NOT raise TransitionNotAllowed — structured result only.
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True, skip_validation=True),
            )

        assert isinstance(result, dict)
        ticket.refresh_from_db()
        assert ticket.state == expected_state

    def test_skip_validation_from_in_review_recovers_and_ships(self) -> None:
        # #798: IN_REVIEW is now a recoverable reconcile source — a stranded
        # ticket re-ships instead of dead-ending. Still no raw transition.
        self._assert_skip_validation_post_ship_no_raw_transition(Ticket.State.IN_REVIEW, Ticket.State.SHIPPED)

    def test_skip_validation_from_merged_never_raises_raw_transition(self) -> None:
        # MERGED is a genuine terminal (not a reconcile source) — unchanged.
        self._assert_skip_validation_post_ship_no_raw_transition(Ticket.State.MERGED, Ticket.State.MERGED)


class TestEnsurePr(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_no_op_when_branch_has_open_pr(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-x"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-x",
                    status=BranchStatus.OPEN_PR,
                    ahead_count=3,
                    open_pr_url="https://gitlab.com/org/repo/-/merge_requests/42",
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["skipped"] == "open PR exists"
        assert "42" in str(result["url"])
        host.create_pr.assert_not_called()

    def test_no_op_when_branch_synced(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-y"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-y",
                    status=BranchStatus.SYNCED,
                    ahead_count=0,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "synced" in str(result["skipped"])
        host.create_pr.assert_not_called()

    def test_defers_when_branch_not_on_remote(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-z"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-z",
                    status=BranchStatus.UNPUSHED_ORPHAN,
                    ahead_count=2,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "not on remote yet" in str(result["skipped"])
        assert "feat-z" in str(result["hint"])
        host.create_pr.assert_not_called()

    def test_creates_pr_when_pushed_orphan(self) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"url": "https://github.com/souliane/teatree/pull/999"}
        host.current_user.return_value = "souliane"
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod.git, "last_commit_message", return_value=("feat: cool thing", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=5,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["url"] == "https://github.com/souliane/teatree/pull/999"
        assert result["branch"] == "feat-q"
        (spec,) = host.create_pr.call_args.args
        assert spec.draft is False
        assert spec.branch == "feat-q"
        assert spec.repo == "souliane/teatree"
        assert spec.title == "feat: cool thing"

    def test_defers_when_remote_ref_stale_in_pre_push_race(self) -> None:
        """#792: ensure-pr must defer (not raise) on the pre-push stale-remote race.

        ensure-pr runs in the PRE-push hook. The remote branch ref exists at
        an older base (classify_branch => PUSHED_ORPHAN), but THIS push has
        not landed yet, so the forge rejects PR creation with "No commits
        between main and <branch>". Hard-failing aborts the very push that
        would make the PR creatable — a permanent deadlock. ensure-pr must
        DEFER (skip + exit 0) so the push proceeds and the post-push
        ensure-pr opens the PR.
        """
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.create_pr.side_effect = CommandFailedError(
            cmd=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="pull request create failed: GraphQL: No commits between main and feat-q (createPullRequest)",
        )
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod.git, "last_commit_message", return_value=("feat: cool thing", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=3,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        # Deferred, not raised: the push must be allowed to proceed.
        assert "pre-push race" in str(result["skipped"])
        assert "feat-q" in str(result["hint"])
        host.create_pr.assert_called_once()

    def test_other_create_pr_failure_still_raises(self) -> None:
        """A non-race create_pr failure must still surface (no silent swallow)."""
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.create_pr.side_effect = CommandFailedError(
            cmd=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="pull request create failed: GraphQL: API rate limit exceeded",
        )
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod.git, "last_commit_message", return_value=("feat: x", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=3,
                ),
            ),
            pytest.raises(CommandFailedError, match="rate limit"),
        ):
            call_command("pr", "ensure-pr")


class TestPostEvidence(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_delegates_to_code_host(self) -> None:
        """post-evidence posts a PR comment via the code host."""
        host = MagicMock()
        host.list_pr_comments.return_value = []
        host.post_pr_comment.return_value = {"id": 55}
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-evidence", "10", "--body", "All tests pass")

        assert result == {"id": 55}
        host.post_pr_comment.assert_called_once()
        call_kw = host.post_pr_comment.call_args
        assert call_kw.kwargs["pr_iid"] == 10
        assert "All tests pass" in call_kw.kwargs["body"]

    def test_returns_error_without_code_host(self) -> None:
        """post-evidence returns error when no code host configured."""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-evidence", "10")

        assert "error" in result


class TestSweep(TestCase):
    """``pr sweep`` lists all of the user's open PRs across the forge (#466)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_open_prs_for_authenticated_user(self) -> None:
        from teatree.core.overlay import OverlayConfig  # noqa: PLC0415

        host = MagicMock()
        host.list_my_prs.return_value = [
            {
                "iid": 1,
                "title": "feat: x",
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/1",
                "source_branch": "feat-x",
                "target_branch": "main",
            },
            {
                "iid": 2,
                "title": "fix: y",
                "web_url": "https://gitlab.com/org/other/-/merge_requests/2",
                "source_branch": "fix-y",
                "target_branch": "develop",
            },
        ]
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)
        overlay = CommandOverlay()
        # Per-instance config so we don't mutate the class-level default shared by other tests.
        overlay.config = OverlayConfig()
        overlay.config.get_gitlab_username = lambda: "adrien"  # type: ignore[method-assign]

        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert result["author"] == "adrien"
        assert result["count"] == 2
        prs = cast("list[dict[str, object]]", result["prs"])
        assert prs[0]["target_branch"] == "main"
        assert prs[1]["target_branch"] == "develop"
        host.list_my_prs.assert_called_once_with(author="adrien")

    def test_falls_back_to_current_user_when_no_username_configured(self) -> None:
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.list_my_prs.return_value = []
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert result["author"] == "souliane"
        assert result["count"] == 0
        host.list_my_prs.assert_called_once_with(author="souliane")

    def test_returns_error_when_no_code_host_configured(self) -> None:
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert "error" in result

    def test_returns_error_when_username_unresolved(self) -> None:
        host = MagicMock()
        host.current_user.return_value = ""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert "error" in result
        host.list_my_prs.assert_not_called()


class TestCheckShippingGate(TestCase):
    def test_returns_structured_failure_when_no_session(self) -> None:
        # #694 nit 1: no session => no attested work. The gate must return a
        # structured failure (not None), otherwise ``ship()`` raises a raw
        # TransitionNotAllowed from a non-REVIEWED state.
        ticket = Ticket.objects.create()
        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert result["missing"] == ["testing", "reviewing", "retro"]

    def test_returns_none_when_gate_passes(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        assert _check_shipping_gate(ticket) is None

    def test_returns_structured_error_with_missing_phases(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket)

        result = _check_shipping_gate(ticket)

        assert result is not None
        assert result["allowed"] is False
        assert "reviewing" in result["missing"]
        assert "testing" in result["missing"]
        assert "hint" in result


class TestResolveBaseUrl(TestCase):
    def test_returns_default_when_worktree_is_none(self) -> None:
        assert _resolve_base_url(None) == "http://127.0.0.1:8000"

    def test_prefers_frontend_url(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"urls": {"frontend": "http://localhost:4201", "backend": "http://localhost:8001"}},
        )
        assert _resolve_base_url(worktree) == "http://localhost:4201"

    def test_falls_back_to_backend(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"urls": {"backend": "http://localhost:8001"}},
        )
        assert _resolve_base_url(worktree) == "http://localhost:8001"

    def test_falls_back_to_localhost_when_no_urls(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        assert _resolve_base_url(worktree) == "http://127.0.0.1:8000"


class TestRunVisualQAGate(TestCase):
    def _ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/wt", branch="feat-x")
        return ticket

    def test_skipped_run_does_not_pollute_extra(self) -> None:
        ticket = self._ticket()
        clean = visual_qa.VisualQAReport(targets=[], skipped_reason="no frontend changes")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=clean),
        ):
            assert _run_visual_qa_gate(ticket) is None

        ticket.refresh_from_db()
        assert "visual_qa" not in ticket.extra

    def test_records_summary_when_pages_checked(self) -> None:
        ticket = self._ticket()
        page = visual_qa.PageResult(url="http://x/", screenshot_path=".t3/visual_qa/00-root.png")
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            assert _run_visual_qa_gate(ticket) is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["pages_checked"] == 1
        assert ticket.extra["visual_qa"]["errors"] == 0

    def test_returns_error_when_findings(self) -> None:
        ticket = self._ticket()
        page = visual_qa.PageResult(
            url="http://x/",
            errors=[visual_qa.PageError(url="http://x/", kind="page", message="boom")],
        )
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket)

        assert result is not None
        assert result["allowed"] is False
        assert "1 blocking finding" in result["error"]
        assert "## Visual QA" in result["report_markdown"]

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["errors"] == 1

    def test_resolves_invoking_worktree_not_stale_first(self) -> None:
        """#776 N1: visual QA inspects the invoking workstream's repo.

        Same ``worktrees.first()`` root cause as the ship-branch fix,
        same path, residual on the reused-multi-workstream ticket #776
        targets. With ``ship_invoking_branch`` recorded, the gate must
        scan the matching worktree's repo, not the stale earliest row.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/776")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/repo-pr-a", branch="s-776-pr-a-merged")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/repo-pr-b", branch="s-776-pr-b-current")
        ticket.extra = {"ship_invoking_branch": "s-776-pr-b-current"}
        ticket.save(update_fields=["extra"])
        captured: dict[str, str] = {}

        def fake_changed_files(*, repo: str) -> list[str]:
            captured["repo"] = repo
            return []

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "changed_files", side_effect=fake_changed_files),
            patch.object(
                visual_qa, "evaluate", return_value=visual_qa.VisualQAReport(targets=[], skipped_reason="none")
            ),
        ):
            assert _run_visual_qa_gate(ticket) is None

        # The invoking PR-B repo is scanned — NOT the stale PR-A first() row.
        assert captured["repo"] == "/tmp/repo-pr-b"

    def test_skip_reason_propagates(self) -> None:
        ticket = self._ticket()
        captured: dict[str, str] = {}

        def fake_evaluate(**kwargs: object) -> visual_qa.VisualQAReport:
            captured["skip_reason"] = str(kwargs.get("skip_reason", ""))
            return visual_qa.VisualQAReport(targets=[], skipped_reason="--skip: my reason")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", side_effect=fake_evaluate),
        ):
            assert _run_visual_qa_gate(ticket, skip_reason="my reason") is None

        assert captured["skip_reason"] == "my reason"


class TestPrCreateNoCommitsAheadGuard(TestCase):
    """#788: pr create must fail loudly on a 0-commits-ahead branch.

    No hollow ``shipped`` — instead of advancing the FSM and deferring
    an empty-diff failure into the async ship worker, the branch must
    have ≥1 commit ahead of its base.
    """

    @staticmethod
    def _git(repo: str, *args: str) -> None:
        from teatree.utils import run as run_mod  # noqa: PLC0415

        run_mod.run_checked(["git", "-C", repo, *args])

    def _repo_at_parity_with_base(self, root: str) -> str:
        # A real git repo where the feature branch has NO commits ahead
        # of the base it would be compared against.
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "t")
        (Path(root) / "f.txt").write_text("x", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-q", "-m", "base")
        self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self._git(root, "checkout", "-q", "-b", "feature-parity")
        return "feature-parity"

    def test_branch_at_parity_with_base_does_not_reach_shipped(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            branch = self._repo_at_parity_with_base(root)
            ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
            session = Session.objects.create(ticket=ticket, overlay="test")
            session.visit_phase("testing")
            session.visit_phase("reviewing")
            session.visit_phase("retro")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=root,
                branch=branch,
                extra={"worktree_path": root},
            )

            with (
                patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
                patch.object(pr_command, "validate_pr_metadata", return_value=None),
                patch.object(pr_command.git, "current_branch", return_value=branch),
            ):
                result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

            ticket.refresh_from_db()
            # No hollow success, no FSM transition.
            assert ticket.state == Ticket.State.REVIEWED, f"hollow shipped: state={ticket.state}"
            assert result.get("state") != Ticket.State.SHIPPED
            # Structured no-commits error naming branch + base.
            assert "error" in result
            assert branch in str(result.get("error", "")) or branch in str(result.get("branch", ""))
            assert "base" in result or "ahead" in str(result.get("error", "")).lower()


class TestAssertCommitsAheadOfBase(TestCase):
    """#788 helper unit branches (real tmp git repo per the doctrine)."""

    @staticmethod
    def _git(repo: str, *args: str) -> None:
        from teatree.utils import run as run_mod  # noqa: PLC0415

        run_mod.run_checked(["git", "-C", repo, *args])

    def _wt(self, repo_path: str, branch: str) -> Worktree:
        t = Ticket.objects.create(overlay="test")
        return Worktree.objects.create(
            ticket=t, overlay="test", repo_path=repo_path, branch=branch, extra={"worktree_path": repo_path}
        )

    def test_branch_with_a_commit_ahead_returns_none(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "t@example.com")
            self._git(root, "config", "user.name", "t")
            (Path(root) / "a").write_text("1", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            self._git(root, "checkout", "-q", "-b", "feat-ahead")
            (Path(root) / "b").write_text("2", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "ahead")  # 1 commit ahead of base

            assert _assert_commits_ahead_of_base(self._wt(root, "feat-ahead")) is None

    def test_missing_repo_or_branch_returns_none(self) -> None:
        assert _assert_commits_ahead_of_base(self._wt("", "feat")) is None
        assert _assert_commits_ahead_of_base(self._wt("/tmp/x", "")) is None
