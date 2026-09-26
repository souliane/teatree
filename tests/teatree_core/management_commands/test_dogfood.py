"""Tests for ``t3 dogfood overlay-provision-smoke`` (#1308).

The smoke management command shells out to ``t3 <overlay> ...`` in
production. Tests inject a fake :class:`StepRunner` so the suite never
actually executes any subprocess — the live run would take minutes and
require Docker / overlay infra.

We also verify the DM-on-failure plumbing (``notify_user`` is called
with a body naming the failing step and command) without making a real
Slack call.

The exit-code assertions go through Django's ``call_command`` (the real
production entry path for ``t3 dogfood``) so they pin the *propagated*
code. django-typer swallows a ``typer.Exit`` into a returned value (exit
0); the command therefore ``raise SystemExit(code)`` and these tests use
``pytest.raises(SystemExit)`` to read the genuine code — a ``typer.Exit``
here would regress to a silent exit 0.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from teatree.core.management.commands.dogfood import _worktree_path_resolver
from teatree.loop.dogfood_smoke import SmokeOutcomeKind, SmokeReport, SmokeStep, StepResult
from tests.factories import TicketFactory, WorktreeFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call_smoke(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[str, int]:
    """Invoke ``t3 dogfood overlay-provision-smoke`` via ``call_command``.

    This is the production entry path — django-typer runs the subcommand
    under Django's ``call_command``, which swallows a ``typer.Exit`` into a
    returned value (process exits 0). The command therefore raises
    ``SystemExit(code)`` on a non-zero outcome, so the harness reads the
    propagated code off ``SystemExit`` (and treats a clean return — the PASS
    and dry-run paths — as code 0).
    """
    kwargs = _parse_args(args)
    code = 0
    try:
        call_command("dogfood", "overlay-provision-smoke", **kwargs)
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return captured.out, code


def _parse_args(args: tuple[str, ...]) -> dict[str, object]:
    """Tiny arg parser for the test harness — covers the flags we exercise."""
    kwargs: dict[str, object] = {
        "overlay": "teatree",
        "fixture_ticket_url": "https://github.com/souliane/teatree/issues/1308",
        "variant": "",
        "dry_run": False,
        "notify_on_failure": True,
    }
    for arg in args:
        if arg == "--dry-run":
            kwargs["dry_run"] = True
        elif arg == "--no-notify-on-failure":
            kwargs["notify_on_failure"] = False
        elif arg == "--notify-on-failure":
            kwargs["notify_on_failure"] = True
        elif arg == "--no-overlay":
            kwargs["overlay"] = ""
        elif arg.startswith("--overlay="):
            kwargs["overlay"] = arg.removeprefix("--overlay=")
    return kwargs


class TestDryRun:
    def test_dry_run_lists_steps_without_executing(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("teatree.core.management.commands.dogfood.run_smoke") as mock_run:
            out, code = _call_smoke(capsys, "--dry-run")

        assert code == 0
        assert "dry-run" in out
        assert "workspace_ticket" in out
        assert "worktree_provision" in out
        assert "worktree_teardown" in out
        # The orchestrator must NOT be invoked in dry-run mode.
        mock_run.assert_not_called()


class TestSmokeExecution:
    def test_all_green_exits_zero_and_no_dm(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SmokeReport(outcome=SmokeOutcomeKind.PASS, steps=[])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            out, code = _call_smoke(capsys)

        assert code == 0
        assert "PASS" in out
        mock_notify.assert_not_called()

    def test_provision_failure_exits_eleven_and_dms_user(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_provision", command=("t3", "teatree", "worktree", "provision"))
        result = StepResult(
            step=step,
            returncode=1,
            stderr="dslr alias missing: test-variant",
            stdout="",
            elapsed_seconds=0.1,
        )
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="worktree_provision",
            steps=[result],
        )
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            out, code = _call_smoke(capsys)

        assert code == 11
        assert "provision_failed" in out
        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["failing_step"] == "worktree_provision"
        assert "t3 teatree worktree provision" in kwargs["command_str"]
        assert "dslr alias missing" in kwargs["stderr"]

    def test_start_failure_exits_twelve(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_start", command=("t3", "teatree", "worktree", "start"))
        result = StepResult(step=step, returncode=1, stderr="boom", stdout="", elapsed_seconds=0.1)
        report = SmokeReport(outcome=SmokeOutcomeKind.START_FAILED, failing_step="worktree_start", steps=[result])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure"),
        ):
            _, code = _call_smoke(capsys)
        assert code == 12

    def test_ready_failure_exits_thirteen(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_ready", command=("t3", "teatree", "worktree", "ready"))
        result = StepResult(step=step, returncode=1, stderr="health 503", stdout="", elapsed_seconds=0.1)
        report = SmokeReport(outcome=SmokeOutcomeKind.READY_FAILED, failing_step="worktree_ready", steps=[result])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure"),
        ):
            _, code = _call_smoke(capsys)
        assert code == 13

    def test_timeout_exits_sixteen(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_start", command=("t3", "teatree", "worktree", "start"))
        result = StepResult(step=step, returncode=-1, stderr="", stdout="", elapsed_seconds=120.0, timed_out=True)
        report = SmokeReport(outcome=SmokeOutcomeKind.TIMEOUT, failing_step="worktree_start", steps=[result])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure"),
        ):
            _, code = _call_smoke(capsys)
        assert code == 16

    def test_no_notify_flag_suppresses_dm_on_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_provision", command=("t3", "teatree", "worktree", "provision"))
        result = StepResult(step=step, returncode=1, stderr="boom", stdout="", elapsed_seconds=0.1)
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="worktree_provision",
            steps=[result],
        )
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            _, code = _call_smoke(capsys, "--no-notify-on-failure")

        assert code == 11
        mock_notify.assert_not_called()


class TestExitCodeMapping:
    def _report(self, outcome: SmokeOutcomeKind) -> SmokeReport:
        return SmokeReport(outcome=outcome, failing_step="x", steps=[])

    @pytest.mark.parametrize(
        ("outcome", "expected_code"),
        [
            (SmokeOutcomeKind.PASS, 0),
            (SmokeOutcomeKind.PROVISION_FAILED, 11),
            (SmokeOutcomeKind.START_FAILED, 12),
            (SmokeOutcomeKind.READY_FAILED, 13),
            (SmokeOutcomeKind.TEARDOWN_FAILED, 14),
            (SmokeOutcomeKind.CLEAN_FAILED, 15),
            (SmokeOutcomeKind.TIMEOUT, 16),
            (SmokeOutcomeKind.OVERLAY_RESOLUTION_FAILED, 17),
            (SmokeOutcomeKind.UNKNOWN, 19),
        ],
    )
    def test_outcome_maps_to_distinct_exit_code(self, outcome: SmokeOutcomeKind, expected_code: int) -> None:
        from teatree.core.management.commands.dogfood import _exit_code_for  # noqa: PLC0415

        assert _exit_code_for(outcome) == expected_code

    def test_every_outcome_kind_has_an_exit_code(self) -> None:
        from teatree.core.management.commands.dogfood import _exit_code_for  # noqa: PLC0415  # (#1308)

        # The generic ``.get(outcome, 1)`` fallback must never be what a real
        # outcome hits — an unmapped kind would collide with a generic failure.
        codes = {kind: _exit_code_for(kind) for kind in SmokeOutcomeKind}
        assert 1 not in codes.values()
        assert len(set(codes.values())) == len(codes)


class TestSmokeVariantResolution:
    """#1308 acceptance: pick a NON-identity variant, or say it is uncovered."""

    def test_explicit_variant_wins_and_covers_nothing_extra(self) -> None:
        from teatree.core.management.commands.dogfood import _resolve_smoke_variant  # noqa: PLC0415  # (#1308)

        assert _resolve_smoke_variant("teatree", "handpicked") == ("handpicked", [])

    def test_overlay_alias_is_used_when_no_explicit_variant(self) -> None:
        from teatree.core.management.commands import dogfood  # noqa: PLC0415  # deferred: loads Django settings (#1308)

        with (
            patch.object(dogfood, "get_overlay", return_value=object()),
            patch.object(dogfood, "pick_alias_variant", return_value="acme-metro"),
        ):
            variant, uncovered = dogfood._resolve_smoke_variant("acme-overlay", "")

        assert variant == "acme-metro"
        assert uncovered == []

    def test_identity_only_overlay_reports_the_acceptance_item_uncovered(self) -> None:
        from teatree.core.management.commands import dogfood  # noqa: PLC0415  # deferred: loads Django settings (#1308)

        with (
            patch.object(dogfood, "get_overlay", return_value=object()),
            patch.object(dogfood, "pick_alias_variant", return_value=""),
        ):
            variant, uncovered = dogfood._resolve_smoke_variant("teatree", "")

        assert variant == ""
        assert len(uncovered) == 1
        assert dogfood.ALIAS_VARIANT_UNCOVERED in uncovered[0]
        # The SPECIFIC reason, not just the marker: the unloadable-overlay branch emits the
        # same marker, which is how this test passed while `pick_alias_variant` never ran.
        assert "declares no non-identity variant" in uncovered[0]

    def test_unloadable_overlay_reports_uncovered_instead_of_raising(self) -> None:
        from teatree.core.management.commands import dogfood  # noqa: PLC0415  # deferred: loads Django settings (#1308)

        with patch.object(dogfood, "get_overlay", side_effect=ImproperlyConfigured("no such overlay")):
            variant, uncovered = dogfood._resolve_smoke_variant("ghost", "")

        assert variant == ""
        assert dogfood.ALIAS_VARIANT_UNCOVERED in uncovered[0]
        assert "not loadable" in uncovered[0]

    def test_uncovered_items_reach_the_echoed_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        from teatree.core.management.commands import dogfood  # noqa: PLC0415  # deferred: loads Django settings (#1308)

        report = SmokeReport(outcome=SmokeOutcomeKind.PASS, steps=[], uncovered=["dslr-alias-variant (no alias)"])
        with (
            patch.object(dogfood, "_resolve_smoke_variant", return_value=("", ["dslr-alias-variant (no alias)"])),
            patch.object(dogfood, "run_smoke", return_value=report) as mock_run,
        ):
            out, code = _call_smoke(capsys)

        assert code == 0
        assert "uncovered" in out
        assert "dslr-alias-variant" in out
        assert mock_run.call_args.kwargs["uncovered"] == ["dslr-alias-variant (no alias)"]


class TestNotifyFailureBody:
    def test_body_includes_failing_step_command_and_stderr_tail(self) -> None:
        from teatree.core.management.commands.dogfood import _dm_failure_body  # noqa: PLC0415

        body = _dm_failure_body(
            "dogfood smoke provision_failed at worktree_provision",
            failing_step="worktree_provision",
            command_str="t3 teatree worktree provision",
            stderr="line 1\nline 2\nline 3\nfinal: dslr alias missing",
        )

        assert "worktree_provision" in body
        assert "t3 teatree worktree provision" in body
        assert "dslr alias missing" in body
        # Markdown code fence for the stderr block — Slack renders it as
        # a monospace block.
        assert "```" in body

    def test_body_omits_stderr_block_when_stderr_is_blank(self) -> None:
        from teatree.core.management.commands.dogfood import _dm_failure_body  # noqa: PLC0415

        body = _dm_failure_body(
            "dogfood smoke timeout at worktree_start",
            failing_step="worktree_start",
            command_str="t3 teatree worktree start",
            stderr="   \n  \n",  # only whitespace → no stderr section
        )
        assert "worktree_start" in body
        assert "stderr:" not in body
        assert "```" not in body

    def test_body_omits_stderr_block_when_stderr_is_empty(self) -> None:
        from teatree.core.management.commands.dogfood import _dm_failure_body  # noqa: PLC0415

        body = _dm_failure_body(
            "dogfood smoke timeout at worktree_start",
            failing_step="worktree_start",
            command_str="t3 teatree worktree start",
            stderr="",
        )
        assert "stderr:" not in body


class TestNotifyFailureRouting:
    """Exercise the ``_notify_failure`` helper itself (#1308)."""

    def test_notify_failure_routes_to_verified_delivery_wrapper(self) -> None:
        """The smoke-failure DM goes through the #1181 verified-delivery wrapper."""
        from teatree.core.management.commands.dogfood import _notify_failure  # noqa: PLC0415

        with patch("teatree.messaging.notify_with_fallback") as mock_notify:
            _notify_failure(
                summary_text="dogfood smoke provision_failed",
                failing_step="worktree_provision",
                command_str="t3 teatree worktree provision",
                stderr="boom",
            )

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["idempotency_key"] == "dogfood_smoke:worktree_provision"
        assert "worktree_provision" in mock_notify.call_args.args[0]

    def test_notify_failure_swallows_notify_exception(self) -> None:
        from teatree.core.management.commands.dogfood import _notify_failure  # noqa: PLC0415

        with patch("teatree.messaging.notify_with_fallback", side_effect=RuntimeError("slack down")):
            # Best-effort — the helper must not propagate notify failures.
            _notify_failure(
                summary_text="dogfood smoke provision_failed",
                failing_step="worktree_provision",
                command_str="t3 teatree worktree provision",
                stderr="boom",
            )


class TestOverlayResolution:
    """Cover the active-overlay fallback path (#1308)."""

    def test_missing_overlay_exits_two_and_skips_smoke(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An unresolved overlay short-circuits with exit code 2 and never calls ``run_smoke``."""
        with (
            patch("teatree.core.management.commands.dogfood._resolve_active_overlay", return_value=""),
            patch("teatree.core.management.commands.dogfood.run_smoke") as mock_run,
        ):
            _, code = _call_smoke(capsys, "--no-overlay")

        assert code == 2
        mock_run.assert_not_called()

    def test_resolve_active_overlay_returns_empty_when_no_overlay_registered(self) -> None:
        from teatree.core.management.commands.dogfood import _resolve_active_overlay  # noqa: PLC0415

        with patch("teatree.config.discover_active_overlay", return_value=None):
            assert _resolve_active_overlay() == ""

    def test_resolve_active_overlay_strips_t3_prefix(self) -> None:
        from teatree.core.management.commands.dogfood import _resolve_active_overlay  # noqa: PLC0415

        class _Overlay:
            name = "t3-teatree"

        with patch("teatree.config.discover_active_overlay", return_value=_Overlay()):
            assert _resolve_active_overlay() == "teatree"

    def test_explicit_full_overlay_name_dispatches_under_its_cli_short_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An explicit full/dispatchable ``--overlay`` still dispatches under the CLI short name.

        ``--overlay t3-teatree`` (the dispatchable ``Ticket.overlay`` form the scanner queues) must
        shell out as ``t3 teatree ...`` — the registered CLI sub-app is named after
        :meth:`OverlayEntry.canonical_overlay_name`, not the full entry-point name, so passing the
        full form straight through made every step fail with ``No such command 't3-teatree'``.
        """
        with patch("teatree.core.management.commands.dogfood.run_smoke") as mock_run:
            out, code = _call_smoke(capsys, "--overlay=t3-teatree", "--dry-run")

        assert code == 0
        assert "t3-teatree workspace ticket" not in out
        assert "t3 teatree workspace ticket" in out
        mock_run.assert_not_called()

    def test_short_overlay_name_is_resolved_before_the_variant_lookup(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--overlay teatree`` reaches ``get_overlay`` as its dispatchable ``t3-teatree`` form.

        ``get_overlay`` keys on the registered entry-point name, so handing it the ambient short
        form degraded every run to ``uncovered: ... not loadable`` — a green smoke that had
        silently stopped exercising the variant→tenant path it exists to prove.
        """
        with patch("teatree.core.management.commands.dogfood.run_smoke"):
            out, code = _call_smoke(capsys, "--overlay=teatree", "--dry-run")

        assert code == 0
        assert "not loadable" not in out


class TestFailingStepCommandLookup:
    """Cover the ``command_str`` lookup loop inside the smoke command (#1308)."""

    def test_failing_step_not_in_report_yields_empty_command_str(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Defensive: a corrupted report where ``failing_step`` does not match
        # any captured step's name — the lookup loop falls through without
        # break, leaving ``command_str`` empty.
        step = SmokeStep(name="workspace_ticket", command=("t3", "teatree", "workspace", "ticket"))
        result = StepResult(step=step, returncode=0, stderr="", stdout="", elapsed_seconds=0.01)
        report = SmokeReport(
            outcome=SmokeOutcomeKind.UNKNOWN,
            failing_step="missing_step",
            steps=[result],
        )
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            _, code = _call_smoke(capsys)

        # UNKNOWN maps to exit code 19.
        assert code == 19
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["command_str"] == ""

    def test_failing_step_command_propagated_to_notifier(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Two steps in the report — the second is the failing one, so the
        # lookup loop must iterate past the first to find the command_str.
        first_step = SmokeStep(name="workspace_ticket", command=("t3", "teatree", "workspace", "ticket"))
        failing_step = SmokeStep(
            name="worktree_provision",
            command=("t3", "teatree", "worktree", "provision"),
        )
        first_result = StepResult(step=first_step, returncode=0, stderr="", stdout="", elapsed_seconds=0.01)
        failing_result = StepResult(
            step=failing_step,
            returncode=1,
            stderr="missing dslr",
            stdout="",
            elapsed_seconds=0.1,
        )
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="worktree_provision",
            steps=[first_result, failing_result],
        )

        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            _, code = _call_smoke(capsys)

        assert code == 11
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["command_str"] == "t3 teatree worktree provision"


class TestWorktreePathResolver:
    """The smoke targets the worktree ``workspace_ticket`` created, not the caller's CWD."""

    URL = "https://github.com/souliane/teatree/issues/1308"

    def test_resolver_yields_the_materialised_worktree_path(self, tmp_path: Path) -> None:
        ticket = TicketFactory(issue_url=self.URL, overlay="t3-teatree")
        WorktreeFactory(ticket=ticket, overlay="t3-teatree", extra={"worktree_path": str(tmp_path)})

        resolve = _worktree_path_resolver(issue_url=self.URL, overlay="t3-teatree")

        assert resolve() == str(tmp_path)

    def test_resolver_is_empty_when_the_recorded_checkout_is_gone(self, tmp_path: Path) -> None:
        ticket = TicketFactory(issue_url=self.URL, overlay="t3-teatree")
        WorktreeFactory(ticket=ticket, overlay="t3-teatree", extra={"worktree_path": str(tmp_path / "vanished")})

        assert _worktree_path_resolver(issue_url=self.URL, overlay="t3-teatree")() == ""

    def test_resolver_is_empty_when_no_ticket_row_exists(self) -> None:
        assert _worktree_path_resolver(issue_url=self.URL, overlay="t3-teatree")() == ""

    def test_resolver_keys_on_the_dispatchable_overlay_form(self, tmp_path: Path) -> None:
        ticket = TicketFactory(issue_url=self.URL, overlay="t3-teatree")
        WorktreeFactory(ticket=ticket, overlay="t3-teatree", extra={"worktree_path": str(tmp_path)})

        assert _worktree_path_resolver(issue_url=self.URL, overlay="teatree")() == ""

    def test_smoke_run_injects_the_resolver(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("teatree.core.management.commands.dogfood.run_smoke") as mock_run:
            mock_run.return_value = SmokeReport()
            _call_smoke(capsys)

        assert callable(mock_run.call_args.kwargs["resolve_worktree_path"])
