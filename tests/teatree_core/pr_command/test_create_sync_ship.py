import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

from teatree.core.management.commands import _pr_preview
from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Session, Ticket, Worktree

from ._shared import _MOCK_OVERLAY, _SHIP_BACKEND, _shippable_ticket

# A commit subject + body that satisfy the deterministic MR title/description
# gate (#1540), so a test exercising an unrelated invariant (FSM reconcile, no
# raw transition) is not tripped by the now-active format check.
_CONFORMING_COMMIT = ("feat(ship): recover and ship", "## What\nx\n\n## Why\ny")


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
            patch.object(_pr_preview.git, "last_commit_message", return_value=_CONFORMING_COMMIT),
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

    def test_skip_validation_still_enforces_mr_format_check(self) -> None:
        """Skip the heavy gates, still enforce the MR format check.

        ``--skip-validation`` skips the HEAVY gates but NOT the cheap,
        deterministic MR title/description format check — a non-compliant
        title must not slip onto GitLab via the bypass. Only the explicit
        ``--skip-mr-format-check`` opt-in disables the format check too.
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
        fmt_error = pr_command.PrValidationError(error="PR validation failed", details=["bad title"])
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "validate_pr_metadata", return_value=fmt_error) as fmt,
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True, skip_validation=True),
            )
        fmt.assert_called_once()
        assert result == fmt_error
        ship_mock.call.assert_not_called()

    def test_skip_mr_format_check_disables_the_format_check_too(self) -> None:
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
            patch.object(pr_command, "validate_pr_metadata") as fmt,
            patch("teatree.core.tasks.execute_ship", ship_mock),
        ):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr", "create", str(ticket.id), sync=True, skip_validation=True, skip_mr_format_check=True
                ),
            )
        fmt.assert_not_called()
        assert result["ok"] is True

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
            patch.object(_pr_preview.git, "last_commit_message", return_value=_CONFORMING_COMMIT),
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


class TestPrCreateSyncShipAtomic(TestCase):
    """#838: a ``ShipExecutor.run()`` exception during ``pr create --sync``.

    Reproduces the real-world failure (ticket 195): the ship FSM
    transition committed, then ``ShipExecutor.run()`` raised inside
    ``execute_ship`` (a ``git push`` precondition failure surfaces as
    ``CommandFailedError``). Pre-fix this left ``Ticket.state ==
    SHIPPED`` with no push and no PR, and the real error was swallowed —
    the CLI only saw a bare ``rc=1`` from the manage.py-wrapper
    recursion. The regression asserts both halves:

    Atomicity: the FSM is NOT left in ``SHIPPED`` (no partial state) —
    reverting the atomicity fix turns that assertion RED.

    Surfacing: the real underlying git error is visible in the
    structured CLI result, not masked as a bare ``rc=1`` — reverting
    the surfacing fix turns that assertion RED.

    ``execute_ship`` is deliberately NOT mocked — the real task path is
    what masks the error and commits the partial state, so a faithful
    repro must exercise it. Only the unstoppable external (the ``git
    push`` subprocess inside the ship runner) is stubbed to raise.
    """

    @override_settings(**_SHIP_BACKEND)
    def test_ship_executor_exception_is_atomic_and_surfaced(self) -> None:
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        ticket = _shippable_ticket()
        host = MagicMock()
        host.current_user.return_value = "tester"

        def _raise_push(*_args: object, **_kwargs: object) -> None:
            raise CommandFailedError(
                ["git", "-C", "/tmp/backend", "push", "--set-upstream", "origin", "feature-branch"],
                1,
                "",
                "! [rejected] feature-branch -> feature-branch (non-fast-forward)",
            )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.git.push", side_effect=_raise_push),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )

        # (a) Atomicity: the ship FSM advance must NOT have been
        #     committed — no push happened, so no partial SHIPPED state.
        ticket.refresh_from_db()
        assert ticket.state != Ticket.State.SHIPPED, (
            f"FSM left in partial SHIPPED state with no push/PR: {ticket.state}"
        )
        assert ticket.state == Ticket.State.REVIEWED, ticket.state

        # (b) Surfacing: the real git error is in the structured result,
        #     not masked as a bare ``rc=1``.
        assert result.get("ok") is False, result
        detail = str(result.get("detail", ""))
        assert "non-fast-forward" in detail, detail
        assert detail.strip() not in {"", "command failed (rc=1)"}, detail

    @override_settings(**_SHIP_BACKEND)
    def test_ship_executor_structured_failure_is_atomic(self) -> None:
        """#860: a non-raising ``RunnerResult(ok=False)`` must roll back too.

        ``#838`` only treats an *exception* as the rollback trigger.
        ``ShipExecutor.run()`` also has non-raising precondition exits
        (``"no code host configured"``, ``"no worktree on ticket"``,
        ``"branch ... already merged into base"``) that return
        ``RunnerResult(ok=False)`` instead of raising. ``execute_ship``
        then returns a normal ``{"ok": False}`` dict, its
        ``transaction.atomic()`` commits, and pre-fix the outer
        ``_ship_sync`` transaction committed too — leaving the FSM in a
        partial ``SHIPPED`` with no push and no PR. This regression drives
        the structured-failure path through the real (un-mocked)
        ``execute_ship`` and asserts the FSM is NOT left at ``SHIPPED``.
        """
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            # No code host configured -> ShipExecutor.run() returns
            # RunnerResult(ok=False) WITHOUT raising.
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )

        # (a) Atomicity: a structured ship failure must roll the ``ship()``
        #     advance back — no partial SHIPPED with no push/PR.
        ticket.refresh_from_db()
        assert ticket.state != Ticket.State.SHIPPED, (
            f"FSM left in partial SHIPPED state with no push/PR: {ticket.state}"
        )
        assert ticket.state == Ticket.State.REVIEWED, ticket.state

        # (b) Surfacing: the real precondition cause is visible.
        assert result.get("ok") is False, result
        assert "no code host configured" in str(result.get("detail", "")), result


def _dirty_git_worktree_ticket(tmp_path: Path) -> tuple[Ticket, Path]:
    """A REVIEWED ticket whose worktree is a real, tracked-dirty git repo.

    ``_shippable_ticket`` points the worktree at a non-git ``/tmp`` path so
    the #884 preflight fails open (returns None). To exercise the refusal we
    need a genuine git repo with an uncommitted *tracked* modification.
    """
    repo_dir = tmp_path / "backend"
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, env=env, capture_output=True)  # noqa: S607

    git("init", "--initial-branch=main")
    (repo_dir / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-m", "seed")
    git("checkout", "-b", "feature-branch")
    (repo_dir / "code.py").write_text("v1\n")
    git("add", "code.py")
    git("commit", "-m", "add code")

    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
    session = Session.objects.create(ticket=ticket, overlay="test")
    session.visit_phase("testing")
    session.visit_phase("reviewing")
    session.visit_phase("retro")
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path=str(repo_dir),
        branch="feature-branch",
        extra={"worktree_path": str(repo_dir)},
    )
    # Uncommitted TRACKED change — the #884 refusal trigger.
    (repo_dir / "code.py").write_text("v2 uncommitted\n")
    return ticket, repo_dir


class TestPrCreateDirtyWorktreeStructuredFailure(TestCase):
    """#884 BLOCKER 1b: a dirty worktree at ship returns a structured failure.

    ``DirtyWorktreeError`` subclasses ``InvalidTransitionError`` (a
    ``ValueError``), NOT django-fsm ``TransitionNotAllowed``. Pre-fix
    ``_do_ship_transition``'s ``except TransitionNotAllowed`` did not catch
    it, so the default (async) ``pr create`` path let the exception escape
    the command instead of returning the structured ``ShippingGateFailure``
    contract every other gate refusal uses. These tests drive the real
    ``ship()`` → ``_refuse_if_worktree_dirty`` refusal and assert a
    structured failure dict, not an exception escaping ``call_command``.
    """

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_async_path_returns_structured_failure_not_exception(self) -> None:
        ticket, _repo = _dirty_git_worktree_ticket(self._tmp_path)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        # Structured failure (not a crash): the ShippingGateFailure shape.
        assert result.get("allowed") is False, result
        assert result.get("error"), result
        assert "missing" in result, result
        # The real cause names the dirty worktree / uncommitted changes.
        assert "uncommitted tracked" in str(result.get("error", "")), result
        # FSM did NOT advance — the refusal rolled the ship() advance back.
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED, ticket.state

    @override_settings(**_SHIP_BACKEND)
    def test_sync_path_returns_structured_failure_not_exception(self) -> None:
        ticket, _repo = _dirty_git_worktree_ticket(self._tmp_path)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )

        # Structured failure surfaced (not an exception escaping the command).
        assert result.get("allowed") is False, result
        assert "uncommitted tracked" in str(result.get("error", "")), result
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED, ticket.state
