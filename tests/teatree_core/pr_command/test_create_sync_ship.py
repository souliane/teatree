import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import TestCase, TransactionTestCase, override_settings

from teatree.core.gates import pr_budget_gate
from teatree.core.management.commands import _pr_preview
from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands._ship import gates as ship_gates
from teatree.core.models import PullRequest, Session, Ticket, Worktree

from ._shared import (
    _CONFORMING_COMMIT,
    _MOCK_OVERLAY,
    _PR_URL,
    _SHIP_BACKEND,
    _code_host,
    _gates_pass,
    _ship_externals,
    _shippable_ticket,
)

pytestmark = pytest.mark.usefixtures("readable_ship_tree")


class TestPrCreateSyncShip(TestCase):
    """`pr create --sync` runs the ship inline; async warns it is queued (#708)."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_sync_pushes_opens_the_pr_and_requests_review(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with _ship_externals(host) as push, _gates_pass():
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        push.assert_called_once()
        assert result == {
            "ticket_id": ticket.pk,
            "state": Ticket.State.REVIEW_REQUESTED,
            "synced": True,
            "ok": True,
            "detail": _PR_URL,
        }

    def test_sync_reports_ship_failure_without_raising(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with _ship_externals(host) as push, _gates_pass():
            push.return_value = MagicMock(ok=False, failure="rejected", detail="push rejected")
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        host.create_pr.assert_not_called()
        assert result["synced"] is True
        assert result["ok"] is False
        assert "push rejected" in str(result["detail"])
        assert result["state"] == Ticket.State.SELF_REVIEWED

    def test_unexpected_preflight_error_is_structured(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with (
            _ship_externals(host) as push,
            _gates_pass(),
            patch(
                "teatree.core.models.ticket_scheduling.collect_dirty_worktree_paths",
                side_effect=RuntimeError("git status failed"),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert result["synced"] is True
        assert result["ok"] is False
        assert "git status failed" in str(result["detail"])
        push.assert_not_called()
        host.create_pr.assert_not_called()

    def test_sync_adopts_pr_url_recorded_during_gates(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        def record_existing_pr(*_args: object, **_kwargs: object) -> None:
            Ticket.objects.filter(pk=ticket.pk).update(
                extra={"pr_url_by_branch": {"feature-branch": _PR_URL}, "pr_urls": [_PR_URL]}
            )

        with (
            _ship_externals(host) as push,
            patch.object(pr_command, "_run_visual_qa_gate", side_effect=record_existing_pr),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert result["ok"] is True
        assert result["detail"] == _PR_URL
        push.assert_not_called()
        host.create_pr.assert_not_called()
        host.get_pr_open_state.assert_not_called()

    def test_sync_retry_adopts_its_recorded_pr_with_budget_one(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with (
            _ship_externals(host) as push,
            _gates_pass(),
            patch.object(ship_gates.git, "remote_slug", return_value="acme/backend"),
            patch.object(pr_budget_gate, "resolve_pr_budget", return_value=1),
            patch.object(pr_budget_gate, "cached_forge_open_pr_urls_for_ticket", return_value=set()),
            patch.object(Ticket, "request_review", side_effect=OperationalError("database is locked")),
        ):
            first = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        with (
            _ship_externals(host) as retry_push,
            _gates_pass(),
            patch.object(ship_gates.git, "remote_slug", return_value="acme/backend"),
            patch.object(pr_budget_gate, "resolve_pr_budget", return_value=1),
            patch.object(pr_budget_gate, "cached_forge_open_pr_urls_for_ticket", return_value=set()),
        ):
            second = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert first["ok"] is False
        assert (
            first["detail"]
            == "PR https://github.com/acme/backend/pull/7 opened but the FSM advance failed: database is locked"
        )
        assert second["ok"] is True
        assert second["detail"] == _PR_URL
        push.assert_called_once()
        retry_push.assert_not_called()
        host.create_pr.assert_called_once()

    def test_sync_refreshes_ticket_before_preflight(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        def reopen_during_gates(*_args: object, **_kwargs: object) -> None:
            Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)

        with (
            _ship_externals(host) as push,
            patch.object(pr_command, "_run_visual_qa_gate", side_effect=reopen_during_gates),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert result["allowed"] is False
        assert "merged" in str(result["error"])
        push.assert_not_called()
        host.create_pr.assert_not_called()

    def test_commit_error_after_pr_open_reports_the_url(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with (
            _ship_externals(host),
            _gates_pass(),
            patch.object(Ticket, "request_review", side_effect=OperationalError("database is locked")),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert result["ok"] is False
        assert _PR_URL in str(result["detail"])
        assert "database is locked" in str(result["detail"])
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SELF_REVIEWED
        assert ticket.extra["pr_urls"] == [_PR_URL]

    def test_concurrent_completed_ship_reports_success(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        def open_after_concurrent_ship(_spec: object) -> dict[str, str]:
            Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_REQUESTED)
            return {"web_url": _PR_URL}

        host.create_pr.side_effect = open_after_concurrent_ship
        with _ship_externals(host), _gates_pass():
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert result["ok"] is True
        assert result["detail"] == _PR_URL
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert ticket.extra["pr_urls"] == [_PR_URL]

    def test_sync_title_ships_without_staying_pinned_on_the_ticket(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with _ship_externals(host), _gates_pass():
            call_command("pr", "create", str(ticket.id), sync=True, title="feat(ship): pinned title")

        assert host.create_pr.call_args.args[0].title == "feat(ship): pinned title"
        ticket.refresh_from_db()
        assert "pr_title_override" not in ticket.extra

    def test_sync_title_is_not_left_pinned_when_the_push_fails(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        with _ship_externals(host) as push, _gates_pass():
            push.return_value = MagicMock(ok=False, failure="rejected", detail="push rejected")
            call_command("pr", "create", str(ticket.id), sync=True, title="feat(ship): pinned title")

        ticket.refresh_from_db()
        assert "pr_title_override" not in ticket.extra

    def test_pr_opened_but_reopened_meanwhile_reports_failure_and_keeps_the_url(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()

        def _create_while_reopened(_spec: object) -> dict[str, str]:
            Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.WORK_STARTED)
            return {"web_url": _PR_URL}

        host.create_pr.side_effect = _create_while_reopened
        with _ship_externals(host), _gates_pass():
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert result["ok"] is False
        assert _PR_URL in str(result["detail"])
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        assert ticket.extra["pr_urls"] == [_PR_URL]

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
        reconcile, so ``ship()`` failed from a non-SELF_REVIEWED state — the
        gate-fixer bootstrap exception was structurally broken (it could
        never actually ship the very tickets it exists for). The skip
        path now walks the FSM to SELF_REVIEWED via ``reconcile_reviewed`` so
        ``ship()`` is legal. RED on the pre-fix body (returns the
        "Cannot ship from state" gate failure); GREEN once the skip path
        reconciles.
        """
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.WORK_STARTED,
            extra={"visual_qa": {"report_markdown": "## Visual QA\n\n:x: stale failure"}},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        with (
            _ship_externals(_code_host()),
            patch.object(_pr_preview.git, "last_commit_message", return_value=_CONFORMING_COMMIT),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True, skip_validation=True),
            )

        # The authorized bypass now ships: FSM reconciled to SELF_REVIEWED,
        # ship() legal, no "Cannot ship from state" failure.
        assert result.get("allowed") is not False, result
        assert result["ok"] is True
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert "visual_qa" not in ticket.extra

    def test_skip_validation_still_enforces_mr_format_check(self) -> None:
        """Skip the heavy gates, still enforce the MR format check.

        ``--skip-validation`` skips the HEAVY gates but NOT the cheap,
        deterministic MR title/description format check — a non-compliant
        title must not slip onto GitLab via the bypass. Only the explicit
        ``--skip-mr-format-check`` opt-in disables the format check too.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.WORK_STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        host = _code_host()
        fmt_error = pr_command.PrValidationError(error="PR validation failed", details=["bad title"])
        with (
            _ship_externals(host) as push,
            patch.object(pr_command, "validate_pr_metadata", return_value=fmt_error) as fmt,
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True, skip_validation=True),
            )
        fmt.assert_called_once()
        assert result == fmt_error
        push.assert_not_called()
        host.create_pr.assert_not_called()

    def test_skip_mr_format_check_disables_the_format_check_too(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.WORK_STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        with (
            _ship_externals(_code_host()),
            patch.object(pr_command, "validate_pr_metadata") as fmt,
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
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.WORK_STARTED)
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
    ) -> dict[str, object]:
        """``--skip-validation`` from a post-ship state never raises raw (#694/#748).

        It must degrade to a structured dict result rather than raising a
        raw ``TransitionNotAllowed``.

        The resulting FSM state depends on the start state: ``MERGED`` is a
        genuine terminal (no reconcile source) so it stays unchanged;
        ``REVIEW_REQUESTED`` is now a recoverable source (#798) so a gate/auth-
        passing ticket reconciles ``REVIEW_REQUESTED → SELF_REVIEWED`` and re-ships
        back to ``REVIEW_REQUESTED``. The safety invariant (no raw raise) holds for both.
        """
        ticket = Ticket.objects.create(overlay="test", state=post_state)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        with (
            _ship_externals(_code_host()),
            patch.object(_pr_preview.git, "last_commit_message", return_value=_CONFORMING_COMMIT),
        ):
            # Must NOT raise TransitionNotAllowed — structured result only.
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True, skip_validation=True),
            )

        assert isinstance(result, dict)
        ticket.refresh_from_db()
        assert ticket.state == expected_state
        return result

    def test_skip_validation_from_in_review_recovers_and_ships(self) -> None:
        # #798: REVIEW_REQUESTED is now a recoverable reconcile source — a stranded
        # ticket re-ships instead of dead-ending. Still no raw transition.
        result = self._assert_skip_validation_post_ship_no_raw_transition(
            Ticket.State.REVIEW_REQUESTED, Ticket.State.REVIEW_REQUESTED
        )
        assert result["ok"] is True
        assert result["detail"] == _PR_URL

    def test_skip_validation_from_merged_never_raises_raw_transition(self) -> None:
        # MERGED is a genuine terminal (not a reconcile source) — unchanged.
        self._assert_skip_validation_post_ship_no_raw_transition(Ticket.State.MERGED, Ticket.State.MERGED)


class TestPrCreateSyncShipAtomic(TestCase):
    """#838: a ``ShipExecutor.run()`` exception during ``pr create --sync``.

    Reproduces the real-world failure (ticket 195): the ship FSM
    transition committed, then ``ShipExecutor.run()`` raised inside
    ``execute_ship`` (a ``git push`` precondition failure surfaces as
    ``CommandFailedError``). Pre-fix this left ``Ticket.state ==
    PR_OPENED`` with no push and no PR, and the real error was swallowed —
    the CLI only saw a bare ``rc=1`` from the manage.py-wrapper
    recursion. The regression asserts both halves:

    Atomicity: the FSM is NOT left in ``PR_OPENED`` (no partial state) —
    reverting the atomicity fix turns that assertion RED.

    Surfacing: the real underlying git error is visible in the
    structured CLI result, not masked as a bare ``rc=1`` — reverting
    the surfacing fix turns that assertion RED.

    The sync path runs the real ``ShipExecutor``. Only the external
    ``git push`` subprocess is stubbed to raise.
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
            patch("teatree.core.runners.ship.branch_is_landed", return_value=False),
            patch("teatree.core.runners.ship.push_branch", side_effect=_raise_push),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id), sync=True),
            )

        # (a) Atomicity: the ship FSM advance must NOT have been
        #     committed — no push happened, so no partial PR_OPENED state.
        ticket.refresh_from_db()
        assert ticket.state != Ticket.State.PR_OPENED, (
            f"FSM left in partial PR_OPENED state with no push/PR: {ticket.state}"
        )
        assert ticket.state == Ticket.State.SELF_REVIEWED, ticket.state

        # (b) Surfacing: the real git error is in the structured result,
        #     not masked as a bare ``rc=1``.
        assert result.get("ok") is False, result
        detail = str(result.get("detail", ""))
        assert "non-fast-forward" in detail, detail
        assert detail.strip() not in {"", "command failed (rc=1)"}, detail

    @override_settings(**_SHIP_BACKEND)
    def test_ship_executor_structured_failure_is_atomic(self) -> None:
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
        #     advance back — no partial PR_OPENED with no push/PR.
        ticket.refresh_from_db()
        assert ticket.state != Ticket.State.PR_OPENED, (
            f"FSM left in partial PR_OPENED state with no push/PR: {ticket.state}"
        )
        assert ticket.state == Ticket.State.SELF_REVIEWED, ticket.state

        # (b) Surfacing: the real precondition cause is visible.
        assert result.get("ok") is False, result
        assert "no code host configured" in str(result.get("detail", "")), result


class TestSyncShipIoHoldsNoWriteLock(TransactionTestCase):
    """At top level ``atomic()`` opens ``BEGIN IMMEDIATE``, so a push inside one stalls every control-DB writer."""

    @override_settings(**_SHIP_BACKEND)
    def test_push_and_pr_open_run_outside_any_transaction(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        in_transaction: dict[str, bool] = {}

        def _create_pr(_spec: object) -> dict[str, str]:
            in_transaction["create_pr"] = connection.in_atomic_block
            return {"web_url": _PR_URL}

        def _push(**_kwargs: object) -> MagicMock:
            in_transaction["push"] = connection.in_atomic_block
            return MagicMock(ok=True)

        host.create_pr.side_effect = _create_pr
        with _ship_externals(host) as push, _gates_pass():
            push.side_effect = _push
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), sync=True))

        assert in_transaction == {"push": False, "create_pr": False}
        assert result["ok"] is True
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert ticket.extra["pr_urls"] == [_PR_URL]
        assert PullRequest.objects.get(url=_PR_URL).create_verification == PullRequest.CreateVerification.CONFIRMED
        assert host.create_pr.call_count == 1


def _dirty_git_worktree_ticket(tmp_path: Path) -> tuple[Ticket, Path]:
    """A SELF_REVIEWED ticket whose worktree is a real, tracked-dirty git repo.

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

    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SELF_REVIEWED)
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
        assert ticket.state == Ticket.State.SELF_REVIEWED, ticket.state

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
        assert ticket.state == Ticket.State.SELF_REVIEWED, ticket.state
