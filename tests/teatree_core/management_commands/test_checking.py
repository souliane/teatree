"""Tests for the ``t3 <overlay> checking show`` management command (#1529).

The command reads the prior checkpoint, gathers the window, and advances the
marker AFTER gathering — on the default path only. ``--since`` and
``--no-advance`` must NOT move the marker. ``--json`` returns a parseable
payload; the terse path returns the human view. Overlay scoping is read from
``T3_OVERLAY_NAME``. The checkpoint path is pointed at ``tmp_path`` so the
tests never touch the real DATA_DIR.
"""

import json
from contextlib import AbstractContextManager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.checking import gather_checking_report
from teatree.core.checkpoint import advance_checkpoint, load_checkpoint
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing, DeferredQuestion, Session, Task, TaskAttempt
from teatree.core.models.merge_clear import ClearRequest, MergeAudit, MergeClear
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.overlay import OverlayBase, OverlayMetadata, OverlayRuntime

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "b" * 40


@pytest.fixture
def checkpoint_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "checking_checkpoint_test.json"
    monkeypatch.setattr("teatree.core.checkpoint.checkpoint_path", lambda *_a, **_k: target)
    return target


@pytest.fixture(autouse=True)
def _overlay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_OVERLAY_NAME", "acme")


# ---------------------------------------------------------------------------
# Synthetic overlay doubles for multi-overlay tests
# ---------------------------------------------------------------------------


class _MinimalMeta(OverlayMetadata):
    def get_tool_commands(self):
        return []


class _AcmeOverlayRuntime(OverlayRuntime):
    def run_commands(self, worktree):
        return {}

    def test_command(self, worktree):
        return []


class _AcmeOverlay(OverlayBase):
    runtime = _AcmeOverlayRuntime()
    metadata = _MinimalMeta()

    def get_repos(self) -> list[str]:
        return ["acme/widgets"]

    def get_provision_steps(self, worktree):
        return []


class _BetaOverlayRuntime(OverlayRuntime):
    def run_commands(self, worktree):
        return {}

    def test_command(self, worktree):
        return []


class _BetaOverlay(OverlayBase):
    runtime = _BetaOverlayRuntime()
    metadata = _MinimalMeta()

    def get_repos(self) -> list[str]:
        return ["beta/core"]

    def get_provision_steps(self, worktree):
        return []


def _two_overlay_patch() -> AbstractContextManager[Any]:
    """Patch _discover_overlays to return two synthetic overlays."""
    result: dict[str, OverlayBase] = {"acme": _AcmeOverlay(), "beta": _BetaOverlay()}

    def _fake_discover() -> dict[str, OverlayBase]:
        return result

    _fake_discover.cache_clear = lambda: None

    return patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover)


def _call(*args: str) -> str:
    """Both channels merged: these are CONTENT tests, not channel tests.

    Converted verbs route their human view to stderr through the machine-output
    seam while unconverted siblings still return it for stdout, so a content
    assertion must read both. The channel split itself is asserted by the
    dedicated tests below and by ``tests/quality/test_machine_output_seam.py``.
    """
    buf = StringIO()
    call_command(*args, stdout=buf, stderr=buf)
    return buf.getvalue()


def _merged_ticket(*, number: int = 42, pr_id: int = 7) -> Ticket:
    ticket = Ticket.objects.create(
        overlay="acme",
        issue_url=f"https://github.com/acme/widgets/issues/{number}",
        state=Ticket.State.REVIEW_REQUESTED,
        short_description="widget work",
    )
    clear = MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug="acme/widgets",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            ticket=ticket,
        ),
    )
    MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
    return ticket


class TestCheckingShow:
    """Single-overlay path via ``--this-overlay`` (the old default behavior)."""

    def test_show_returns_terse_text_and_advances_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        out = _call("checking", "show", "--this-overlay")
        assert "Since " in out
        assert "Merged" in out
        assert "[acme/widgets#7]" in out
        # The default path advances the marker.
        assert checkpoint_file.is_file()
        assert load_checkpoint(checkpoint_file) is not None

    def test_nothing_changed_is_a_single_line(self, checkpoint_file: Path) -> None:
        out = _call("checking", "show", "--this-overlay").strip()
        assert out.startswith("Nothing since ")
        assert "\n" not in out

    def test_since_does_not_advance_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        since = (timezone.now() - timedelta(hours=6)).isoformat()
        _call("checking", "show", "--this-overlay", "--since", since)
        assert not checkpoint_file.is_file()

    def test_no_advance_does_not_advance_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        _call("checking", "show", "--this-overlay", "--no-advance")
        assert not checkpoint_file.is_file()

    def test_unparseable_since_exits_nonzero_not_traceback(self, checkpoint_file: Path) -> None:
        # #1652: --since yesterday is rejected with a clean non-zero exit
        # (typer.BadParameter), never a raw datetime ValueError traceback,
        # and the marker is left untouched.
        from typer.testing import CliRunner  # noqa: PLC0415

        from teatree.core.management.commands.checking import Command  # noqa: PLC0415

        result = CliRunner().invoke(Command().typer_app, ["show", "--this-overlay", "--since", "yesterday"])
        assert result.exit_code != 0
        assert not checkpoint_file.is_file()

    def test_json_is_parseable(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        out = _call("checking", "show", "--this-overlay", "--json")
        payload = json.loads(out)
        assert set(payload) == {"since", "merged", "in_flight", "needs_you", "metered", "remaining", "terse"}
        assert payload["merged"]["items"][0]["label"] == "acme/widgets#7"

    def test_overlay_scoping_excludes_other_overlay(
        self, checkpoint_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _merged_ticket(number=1, pr_id=10)
        other = Ticket.objects.create(
            overlay="other",
            issue_url="https://github.com/other/x/issues/2",
            state=Ticket.State.REVIEW_REQUESTED,
        )
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=11,
                slug="other/x",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                ticket=other,
            ),
        )
        MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        out = _call("checking", "show", "--this-overlay", "--json")
        payload = json.loads(out)
        labels = [item["label"] for item in payload["merged"]["items"]]
        assert labels == ["acme/widgets#10"]

    def test_null_ticket_ceremony_merge_scoped_by_resolved_repo(
        self, checkpoint_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NULL-ticket ceremony merge surfaces when its repo is the overlay's (#1559).

        Runs against the real bundled ``t3-teatree`` overlay so the command's
        own ``_resolve_overlay_repos`` derives the repo set (``souliane/teatree``)
        — the ticket-FK JOIN that previously dropped every NULL-ticket CLEAR is
        gone, while a foreign-repo merge stays excluded.
        """
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        for pr_id, slug in ((50, "souliane/teatree"), (51, "other-org/other-repo")):
            clear = MergeClear.objects.create(
                ticket=None,
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result=MergeClear.VerifyResult.GREEN,
                blast_class=MergeClear.BlastClass.LOGIC,
            )
            MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        out = _call("checking", "show", "--this-overlay", "--json")
        labels = [item["label"] for item in json.loads(out)["merged"]["items"]]
        assert labels == ["souliane/teatree#50"]

    def test_second_run_after_advance_reports_nothing(self, checkpoint_file: Path) -> None:
        """Gather-then-advance: an immediate second run sees an empty window."""
        _merged_ticket()
        first = _call("checking", "show", "--this-overlay")
        assert "Merged" in first
        second = _call("checking", "show", "--this-overlay").strip()
        assert second.startswith("Nothing since ")

    def test_future_checkpoint_does_not_collapse_window_or_advance_backward(self, checkpoint_file: Path) -> None:
        """A skewed future marker must still report recent events, not collapse.

        A checkpoint written ahead of the clock would yield an empty
        ``[future, now)`` window and then advance the marker forward, silently
        skipping the real events. The guard restores the default lookback so the
        recent merge surfaces, and the monotonic advance leaves the future
        marker untouched (never rewound).
        """
        _merged_ticket()
        future_marker = timezone.now() + timedelta(hours=6)
        advance_checkpoint(future_marker, checkpoint_file)
        out = _call("checking", "show", "--this-overlay")
        assert "Merged" in out  # the recent merge is NOT skipped
        # The marker is not rewound to ``now`` (which would lose the bound).
        assert load_checkpoint(checkpoint_file) == future_marker


def _notify_backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestCheckingRemaining:
    def _open_pr(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/600",
            short_description="finish widget",
            state=Ticket.State.PR_OPENED,
        )
        PullRequest.objects.create(
            ticket=ticket,
            overlay="acme",
            url="https://github.com/acme/widgets/pull/601",
            repo="acme/widgets",
            iid="601",
        )
        return ticket

    def test_open_pr_has_steps_and_unknown_eta_without_history(self, checkpoint_file: Path) -> None:
        self._open_pr()
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert remaining["title"] == "Remaining"
        assert remaining["items"][2]["steps"] == ["independent review", "merge", "deploy"]
        assert remaining["eta"]["median_minutes"] is None
        assert "ETA unknown" in remaining["items"][0]["detail"]
        assert "no default" in remaining["items"][0]["detail"]
        assert "Remaining" in payload["terse"]

    def test_empty_remaining_has_no_current_work_basis(self, checkpoint_file: Path) -> None:
        with patch("teatree.core.checking_remaining.StepHistory.measure") as measure:
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        measure.assert_not_called()
        assert payload["remaining"]["eta"]["basis"] == "no current work in scope"

    def test_dormant_only_scope_keeps_its_json_count(self, checkpoint_file: Path) -> None:
        Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/604", state=Ticket.State.CODED
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert payload["remaining"]["holds"]["dormant"] == 1
        assert payload["remaining"]["items"] == []

    def test_blank_overlay_name_scopes_remaining_like_other_groups(self) -> None:
        self._open_pr()
        now = timezone.now()
        report = gather_checking_report(since=now - timedelta(hours=1), now=now)
        assert report.remaining.total == 1

    def test_finished_ticket_with_live_pr_is_excluded(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.DELIVERED)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert payload["remaining"]["total"] == 0

    def test_no_history_omits_empty_legend_rows_and_formats_queue(self, checkpoint_file: Path) -> None:
        self._open_pr()
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        detail = " ".join(row["detail"] for row in payload["remaining"]["items"][:2])
        assert "None\u2013None" not in detail
        assert "n=0" not in detail
        assert "claimed {" not in detail
        assert "seats {" not in detail

    def test_multiple_prs_count_one_tickets_unknown_eta(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        PullRequest.objects.create(
            ticket=ticket,
            overlay="acme",
            url="https://github.com/acme/widgets/pull/602",
            repo="acme/widgets",
            iid="602",
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert payload["remaining"]["total"] == 2
        assert payload["remaining"]["eta"]["unknown"] == 1

    def test_one_unknown_pr_keeps_its_ticket_unknown(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        PullRequest.objects.create(
            ticket=ticket,
            overlay="acme",
            url="https://github.com/acme/widgets/pull/602",
            repo="acme/widgets",
            iid="602",
        )
        ReviewVerdict.objects.create(
            ticket=ticket,
            pr_id=601,
            slug="acme/widgets",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
        )
        ended = timezone.now() - timedelta(minutes=1)
        for index in range(5):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{610 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="coding",
                status=Task.Status.COMPLETED,
            )
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=10))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        widths = MagicMock(expensive=1, review=0, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert payload["remaining"]["eta"]["unknown"] == 1
        assert payload["remaining"]["eta"]["known"] == 0
        assert payload["remaining"]["eta"]["median_minutes"] is None

    def test_pr_scope_uses_owning_ticket_when_legacy_overlay_is_blank(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        PullRequest.objects.filter(ticket=ticket).update(overlay="")
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert any(item["label"] == "acme/widgets#601" for item in payload["remaining"]["items"])

    def test_completed_tasks_measure_from_admission(self, checkpoint_file: Path) -> None:
        self._open_pr()
        ended = timezone.now() - timedelta(minutes=1)
        for duration in (10, 20, 30, 40, 50):
            ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{700 + duration}"
            )
            task = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="reviewing",
                status=Task.Status.COMPLETED,
            )
            Task.objects.filter(pk=task.pk).update(admitted_at=ended - timedelta(minutes=duration))
            TaskAttempt.objects.create(task=task, ended_at=ended, exit_code=0)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        review = next(row for row in payload["remaining"]["durations"] if row["step"] == "reviewing")
        assert review["median_minutes"] == pytest.approx(30.0)
        assert review["p80_minutes"] == pytest.approx(42.0)
        assert review["n"] == 5
        assert review["window"] == "24h"

    def test_measured_pr_eta_range_and_sources(self, checkpoint_file: Path) -> None:
        self._open_pr()
        now = timezone.now()
        for index, duration in enumerate((10, 20, 30, 40, 200)):
            ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{810 + index}"
            )
            task = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="reviewing",
                status=Task.Status.COMPLETED,
            )
            ended = now - timedelta(minutes=1)
            Task.objects.filter(pk=task.pk).update(admitted_at=ended - timedelta(minutes=duration))
            TaskAttempt.objects.create(task=task, ended_at=ended, exit_code=0)
        for index, (merge_minutes, deploy_minutes) in enumerate(
            zip((1, 2, 3, 4, 5), (10, 20, 30, 40, 50), strict=True)
        ):
            ticket = Ticket.objects.create(
                overlay="acme",
                issue_url=f"https://github.com/acme/widgets/issues/{820 + index}",
                state=Ticket.State.DELIVERED,
            )
            merged = now - timedelta(minutes=55)
            clear = MergeClear.objects.create(
                ticket=ticket,
                pr_id=830 + index,
                slug="acme/widgets",
                reviewed_sha=_SHA,
                reviewer_identity="reviewer",
                gh_verify_result=MergeClear.VerifyResult.GREEN,
                blast_class=MergeClear.BlastClass.LOGIC,
                issued_at=merged - timedelta(minutes=merge_minutes),
            )
            MergeAudit.objects.create(
                clear=clear,
                merged_sha=_SHA,
                required_checks_status="success",
                merged_at=merged,
            )
            transition = TicketTransition.objects.create(
                ticket=ticket,
                from_state=Ticket.State.MERGED,
                to_state=Ticket.State.DELIVERED,
            )
            TicketTransition.objects.filter(pk=transition.pk).update(
                created_at=merged + timedelta(minutes=deploy_minutes)
            )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert remaining["eta"]["median_minutes"] == pytest.approx(63)
        assert remaining["eta"]["p80_minutes"] == pytest.approx(118.2)
        assert "1h03m\u20131h58m" in remaining["items"][0]["detail"]
        assert "1h03m\u20131h58m" in payload["terse"]
        assert all(item["detail"] in payload["terse"] for item in remaining["items"])
        assert {row["step"] for row in remaining["durations"]} == {"reviewing", "merge", "deploy"}
        assert all(row["n"] == 5 and row["window"] == "24h" for row in remaining["durations"])

    def test_cancel_is_not_a_failed_run_in_either_scope(self, checkpoint_file: Path) -> None:
        for number, error, result in (
            (801, "cancelled: operator", {}),
            (802, "agent crashed", {}),
            (803, "ToolError: Operation cancelled: upstream connection reset", {}),
            (804, "agent crashed", {"cancel_source": "owner"}),
        ):
            ticket = Ticket.objects.create(
                overlay="acme",
                issue_url=f"https://github.com/acme/widgets/issues/{number}",
                short_description=str(number),
            )
            task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
            TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error=error, result=result)
        for flags in (("--this-overlay",), ()):
            with _two_overlay_patch():
                payload = json.loads(_call("checking", "show", *flags, "--no-advance", "--json"))
            labels = [item["label"] for item in payload["needs_you"]["items"]]
            assert "#801" not in labels
            assert "#802" in labels
            assert "#803" in labels
            assert "#804" not in labels

    def test_remaining_alone_does_not_notify(self, checkpoint_file: Path) -> None:
        self._open_pr()
        with patch("teatree.core.management.commands.checking.notify_user") as notify:
            _call("checking", "show", "--this-overlay", "--no-advance", "--notify")
        notify.assert_not_called()

    def test_queue_shows_counts_age_and_seats(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/850",
            short_description="queue work",
            state=Ticket.State.PLAN_RECORDED,
        )
        for index in range(4):
            task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="code")
            Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=80 - index))
        widths = MagicMock(expensive=2, review=1, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        queue = payload["remaining"]["queue"]
        assert queue["pending"] == {"coding": 4}
        assert queue["claimed"] == {}
        assert queue["oldest_pending_minutes"] == pytest.approx(80, abs=1)
        assert queue["oldest_pending_phase"] == "coding"
        assert queue["seats"] == {"expensive": 2, "review": 1, "retiring": 1}
        assert "queued, 0 ahead" in payload["remaining"]["items"][2]["detail"]
        ended = timezone.now() - timedelta(minutes=1)
        for index in range(5):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{980 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="coding",
                status=Task.Status.COMPLETED,
            )
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=30))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        widths.expensive = 0
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            disabled = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in disabled["remaining"]["items"] if row["label"] == "#850")
        assert item["eta"]["median_minutes"] is None
        assert "no seats for expensive lane" in item["detail"]

    def test_queue_position_and_seat_wait_follow_admission_priority(self, checkpoint_file: Path) -> None:
        now = timezone.now()
        older = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/851", state=Ticket.State.NOT_STARTED
        )
        old_task = Task.objects.create(ticket=older, session=Session.objects.create(ticket=older), phase="planning")
        Task.objects.filter(pk=old_task.pk).update(created_at=now - timedelta(hours=2))
        started = self._open_pr()
        pr_task = Task.objects.create(ticket=started, session=Session.objects.create(ticket=started), phase="coding")
        Task.objects.filter(pk=pr_task.pk).update(created_at=now - timedelta(hours=1))
        for index in range(5):
            ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{852 + index}"
            )
            sample = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="coding",
                status=Task.Status.COMPLETED,
            )
            ended = now - timedelta(minutes=1)
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=10))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        widths = MagicMock(expensive=1, review=1, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
            Task.objects.filter(pk=old_task.pk).update(status=Task.Status.COMPLETED)
            without_old = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        items = {row["label"]: row for row in payload["remaining"]["items"]}
        pr_without_old = next(row for row in without_old["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert "queued, 0 ahead" in items["acme/widgets#601"]["detail"]
        assert "queued, 1 ahead" in items["#851"]["detail"]
        assert items["acme/widgets#601"]["eta"]["median_minutes"] == pr_without_old["eta"]["median_minutes"]

    def test_admitted_pending_task_does_not_wait_behind_its_own_seat(self, checkpoint_file: Path) -> None:
        now = timezone.now()
        ticket = self._open_pr()
        seated = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        Task.objects.filter(pk=seated.pk).update(admitted_at=now - timedelta(seconds=30))
        for index in range(2):
            waiting_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{860 + index}"
            )
            Task.objects.create(
                ticket=waiting_ticket, session=Session.objects.create(ticket=waiting_ticket), phase="coding"
            )
        for index in range(5):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{870 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="coding",
                status=Task.Status.COMPLETED,
            )
            ended = now - timedelta(minutes=1)
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=10))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        widths = MagicMock(expensive=1, review=1, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert "queued, 0 ahead" in item["detail"]
        assert item["eta"]["median_minutes"] == 60

    def test_unknown_pr_task_phase_uses_ladder_step_history(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="answering")
        now = timezone.now()
        for phase, minutes in (("answering", 5), ("reviewing", 50)):
            for index in range(5):
                sample_ticket = Ticket.objects.create(
                    overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{phase}-{index}"
                )
                sample = Task.objects.create(
                    ticket=sample_ticket,
                    session=Session.objects.create(ticket=sample_ticket),
                    phase=phase,
                    status=Task.Status.COMPLETED,
                )
                ended = now - timedelta(minutes=1)
                Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=minutes))
                TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"] == ["independent review", "merge", "deploy"]
        assert item["eta"]["median_minutes"] == 150
        assert "reviewing" in {row["step"] for row in payload["remaining"]["durations"]}

    def test_unrelated_claimed_phase_does_not_run_the_pr_ladder(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="answering",
            status=Task.Status.CLAIMED,
            claimed_at=timezone.now() - timedelta(minutes=45),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert "now: independent review; then merge, deploy" in item["detail"]
        assert "independent review (running)" not in item["detail"]

    @pytest.mark.parametrize("phase", ["coding", "debugging"])
    def test_claimed_rework_phase_runs_the_rework_step(self, checkpoint_file: Path, phase: str) -> None:
        ticket = self._open_pr()
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase=phase,
            status=Task.Status.CLAIMED,
            claimed_at=timezone.now() - timedelta(minutes=5),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert "now: rework (running)" in item["detail"]

    def test_the_claimed_task_sets_the_step_even_when_an_older_one_is_pending(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        session = Session.objects.create(ticket=ticket)
        older = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        Task.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(hours=2))
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.CLAIMED,
            claimed_at=timezone.now() - timedelta(minutes=5),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert "now: factory review (running)" in item["detail"]

    def test_other_overlay_tasks_do_not_fill_this_overlays_remaining(self, checkpoint_file: Path) -> None:
        other = Ticket.objects.create(
            overlay="beta", issue_url="https://github.com/beta/widgets/issues/1507", state=Ticket.State.CODED
        )
        Task.objects.create(ticket=other, session=Session.objects.create(ticket=other), phase="testing")
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert payload["remaining"]["items"] == []

    def test_merge_ready_pr_does_not_wait_for_headless_coding_seats(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        ReviewVerdict.objects.create(
            ticket=ticket,
            pr_id=601,
            slug="acme/widgets",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
        )
        ended = timezone.now() - timedelta(minutes=1)
        for index in range(5):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{970 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="reviewing",
                status=Task.Status.COMPLETED,
            )
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=30))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
            Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="coding",
            )
        widths = MagicMock(expensive=2, review=1, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"] == ["merge", "deploy"]
        assert item["eta"]["median_minutes"] == 60

        widths.expensive = 0
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            partial = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert partial["remaining"]["eta"]["known"] == 1
        assert partial["remaining"]["eta"]["unknown"] == 5
        assert partial["remaining"]["eta"]["median_minutes"] is None
        assert partial["remaining"]["eta"]["p80_minutes"] is None
        assert partial["remaining"]["items"][0]["detail"].startswith("ETA unknown")

    def test_latest_hold_overrides_older_merge_safe_verdict(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        for verdict, recorded_at in (
            (ReviewVerdict.Verdict.MERGE_SAFE, timezone.now() - timedelta(minutes=2)),
            (ReviewVerdict.Verdict.HOLD, timezone.now() - timedelta(minutes=1)),
        ):
            ReviewVerdict.objects.create(
                ticket=ticket,
                pr_id=601,
                slug="acme/widgets",
                reviewed_sha=_SHA,
                verdict=verdict,
                reviewer_identity="reviewer",
                blast_class=MergeClear.BlastClass.LOGIC,
                gh_verify_result=MergeClear.VerifyResult.GREEN,
                recorded_at=recorded_at,
            )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"] == ["independent review", "merge", "deploy"]

    def test_a_pass_at_the_newest_head_supersedes_a_hold_at_an_older_head(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        for verdict, sha, minutes_ago in (
            (ReviewVerdict.Verdict.HOLD, _SHA, 2),
            (ReviewVerdict.Verdict.MERGE_SAFE, "c" * 40, 1),
        ):
            ReviewVerdict.objects.create(
                ticket=ticket,
                pr_id=601,
                slug="acme/widgets",
                reviewed_sha=sha,
                verdict=verdict,
                reviewer_identity="reviewer",
                blast_class=MergeClear.BlastClass.LOGIC,
                gh_verify_result=MergeClear.VerifyResult.GREEN,
                recorded_at=timezone.now() - timedelta(minutes=minutes_ago),
            )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"] == ["merge", "deploy"]

    def test_running_step_uses_elapsed_survivors_and_overdue_is_unknown(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        ended = timezone.now() - timedelta(minutes=1)
        for index, duration in enumerate((30, 40, 50, 60, 70)):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{900 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="reviewing",
                status=Task.Status.COMPLETED,
            )
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=duration))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        running = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="reviewing",
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + timedelta(hours=1),
        )
        Task.objects.filter(pk=running.pk).update(admitted_at=timezone.now() - timedelta(minutes=45))
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = payload["remaining"]["items"][2]
        assert item["eta"]["median_minutes"] == 165
        assert item["eta"]["p80_minutes"] == 207
        assert "ETA 2h45m\u20133h27m" in item["detail"]
        Task.objects.filter(pk=running.pk).update(admitted_at=timezone.now() - timedelta(minutes=71))
        overdue = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert overdue["remaining"]["eta"]["unknown"] == 1
        assert "running longer than all 5" in overdue["remaining"]["items"][2]["detail"]

    def test_claimed_without_admission_uses_claim_time_not_queue_waves(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="reviewing",
            status=Task.Status.CLAIMED,
            claimed_at=timezone.now() - timedelta(minutes=45),
        )
        ended = timezone.now() - timedelta(minutes=1)
        for index, duration in enumerate((30, 40, 50, 60, 70)):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{1400 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="reviewing",
                status=Task.Status.COMPLETED,
            )
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=duration))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["eta"]["median_minutes"] == 165
        assert "ETA 2h45m" in item["detail"]

    def test_running_step_uses_seven_day_survivors_after_recent_samples_end(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="reviewing",
            status=Task.Status.CLAIMED,
            claimed_at=timezone.now() - timedelta(minutes=45),
        )
        now = timezone.now()
        for index in range(10):
            sample_ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{1450 + index}"
            )
            sample = Task.objects.create(
                ticket=sample_ticket,
                session=Session.objects.create(ticket=sample_ticket),
                phase="reviewing",
                status=Task.Status.COMPLETED,
            )
            ended = now - timedelta(hours=1 if index < 5 else 48)
            duration = 30 if index < 5 else 60
            Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=duration))
            TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["eta"]["median_minutes"] == pytest.approx(105, abs=1)
        assert "7d elapsed survivors" in item["eta"]["basis"]
        assert payload["remaining"]["eta"]["known"] == 1

    def test_failed_task_without_attempt_is_halted(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1500", state=Ticket.State.CODED
        )
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="testing",
            status=Task.Status.FAILED,
            failure_kind="unclassified",
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "#1500")
        assert "halted (unclassified)" in item["detail"]
        assert item["eta"]["median_minutes"] is None
        assert "1 item," in payload["remaining"]["items"][0]["detail"]
        assert "oldest none; seats" in payload["remaining"]["items"][1]["detail"]

    def test_blank_failure_kind_omits_empty_halt_label(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1501", state=Ticket.State.CODED
        )
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket), phase="testing", status=Task.Status.FAILED
        )
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "#1501")
        assert "halted · now:" in item["detail"]
        assert "halted ()" not in item["detail"]

    def test_parked_task_is_not_counted_as_waiting_for_admission(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1502", state=Ticket.State.CODED
        )
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="testing",
            not_before=timezone.now() + timedelta(hours=1),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        item = next(row for row in remaining["items"] if row["label"] == "#1502")
        assert remaining["queue"]["pending"] == {}
        assert remaining["queue"]["oldest_pending_minutes"] is None
        assert "queued" not in item["detail"]

    def test_remaining_cap_applies_to_work_rows_without_counting_meta_rows(self, checkpoint_file: Path) -> None:
        for number in (1503, 1504):
            ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{number}", state=Ticket.State.CODED
            )
            Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="testing")
        now = timezone.now()
        remaining = gather_checking_report(
            since=now - timedelta(hours=1), now=now, overlay_name="acme", cap=1
        ).remaining
        assert remaining.total == 2
        assert [row.label for row in remaining.items[:2]] == ["Total", "Queue"]
        assert len(remaining.items) == 3
        assert "…and 1 more" in "\n".join(remaining.render(cap=1))
        assert "2 items" in remaining.items[0].detail
        assert "oldest" in remaining.items[1].detail

    @pytest.mark.parametrize("count", [4, 5, 7])
    def test_human_table_caps_remaining_work_after_meta_rows(self, checkpoint_file: Path, count: int) -> None:
        for index in range(count):
            ticket = Ticket.objects.create(
                overlay="acme",
                issue_url=f"https://github.com/acme/widgets/issues/{1600 + index}",
                short_description=f"item {index}",
                state=Ticket.State.PR_OPENED,
            )
            PullRequest.objects.create(
                ticket=ticket,
                overlay="acme",
                repo="acme/widgets",
                iid=str(1700 + index),
                url=f"https://github.com/acme/widgets/pull/{1700 + index}",
            )

        out = _call("checking", "show", "--this-overlay", "--no-advance")

        assert "Remaining" in out
        assert "Total" in out
        assert "Queue" in out
        shown = [index for index in range(count) if f"acme/widgets#{1700 + index}" in out]
        assert len(shown) == min(count, 5)
        if count > 5:
            assert "Remaining — 7 (top 5)" in out
            assert "…and 2 more" in out
        else:
            assert "(top 5)" not in out
            assert "…and" not in out

    def test_claimed_shipping_step_uses_shipping_label(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="shipping",
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + timedelta(hours=1),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"][0] == "shipping"
        assert "now: shipping (running)" in item["detail"]
        assert any(row["step"] == "shipping" for row in payload["remaining"]["durations"])

    def test_claimed_shipping_preserves_merge_safe_shortcut(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        ReviewVerdict.objects.create(
            ticket=ticket,
            pr_id=601,
            slug="acme/widgets",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
        )
        Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="shipping",
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + timedelta(hours=1),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"] == ["shipping", "merge", "deploy"]

    def test_running_default_reselects_slowest_surviving_sources(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        ended = timezone.now() - timedelta(minutes=1)
        histories = (
            ("coding", (10,) * 9 + (5000,)),
            ("reviewing", (9,) * 7 + (11, 11, 21)),
            ("debugging", (8,) * 8 + (21, 9000)),
        )
        for group_index, (phase, durations) in enumerate(histories):
            for index, duration in enumerate(durations):
                sample_ticket = Ticket.objects.create(
                    overlay="acme",
                    issue_url=f"https://github.com/acme/widgets/issues/{2000 + group_index * 10 + index}",
                )
                sample = Task.objects.create(
                    ticket=sample_ticket,
                    session=Session.objects.create(ticket=sample_ticket),
                    phase=phase,
                    status=Task.Status.COMPLETED,
                )
                Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=duration))
                TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        running = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="testing",
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + timedelta(hours=1),
        )
        Task.objects.filter(pk=running.pk).update(admitted_at=timezone.now() - timedelta(minutes=20))

        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"] == ["testing", "factory review", "independent review", "merge", "deploy"]
        assert item["eta"]["median_minutes"] == pytest.approx(5018, abs=0.1)
        assert item["eta"]["p80_minutes"] == pytest.approx(7228.2, abs=0.1)
        assert "83h38m\u2013120h28m" in item["detail"]
        assert "testing: default: slowest surviving steps (median coding; p80 debugging)" in item["detail"]

        Task.objects.filter(pk=running.pk).update(admitted_at=timezone.now() - timedelta(minutes=9001))
        overdue = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        overdue_item = next(row for row in overdue["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert overdue_item["eta"]["median_minutes"] is None
        assert overdue_item["eta"]["basis"] == "running longer than all measured fallback samples"

    def test_halts_blocks_and_owner_waits_are_excluded_from_eta(self, checkpoint_file: Path) -> None:
        for number, status in ((860, Task.Status.FAILED), (861, Task.Status.PENDING), (862, Task.Status.PENDING)):
            ticket = Ticket.objects.create(
                overlay="acme",
                issue_url=f"https://github.com/acme/widgets/issues/{number}",
                short_description=f"ticket {number}",
                state=Ticket.State.CODED,
            )
            task = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="testing",
                status=status,
                not_before=timezone.now() + timedelta(hours=1) if number != 860 else None,
            )
            if number == 860:
                TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error="crash")
            if number == 861:
                DeferredQuestion.objects.create(question="Choose scope?", parked_task=task)
                DeferredQuestion.objects.create(question="Choose timeline?", parked_task=task)
        superseded = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/863",
            short_description="recovered",
            state=Ticket.State.CODED,
        )
        old = Task.objects.create(
            ticket=superseded,
            session=Session.objects.create(ticket=superseded),
            phase="coding",
            status=Task.Status.FAILED,
        )
        TaskAttempt.objects.create(task=old, ended_at=timezone.now(), exit_code=1, error="crash")
        Task.objects.create(
            ticket=superseded,
            session=Session.objects.create(ticket=superseded),
            phase="coding",
            status=Task.Status.COMPLETED,
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert remaining["holds"]["halted"] == 1
        assert remaining["holds"]["blocked"] == 1
        assert remaining["holds"]["owner"] == 1
        assert remaining["eta"]["known"] == 0
        assert remaining["eta"]["unknown"] == 0
        assert remaining["eta"]["basis"] == "all current work is held"
        assert "1 blocked" in remaining["items"][0]["detail"]
        assert "#863" not in [item["label"] for item in remaining["items"]]
        details = " ".join(item["detail"] for item in remaining["items"])
        assert "waiting on you" in details
        assert "blocked" in details
        assert "halted" in details
        assert "parked until " in details
        assert "unclassified" in details

    def test_older_halted_is_only_a_dormant_count(self, checkpoint_file: Path) -> None:
        for number, ended_days_ago in ((1400, (9, 0)), (1401, (8,))):
            ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{number}", state=Ticket.State.CODED
            )
            task = Task.objects.create(
                ticket=ticket, session=Session.objects.create(ticket=ticket), phase="testing", status=Task.Status.FAILED
            )
            for days in ended_days_ago:
                TaskAttempt.objects.create(
                    task=task, ended_at=timezone.now() - timedelta(days=days), exit_code=1, error="agent crashed"
                )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert [row["label"] for row in remaining["items"][2:]] == ["#1400"]
        assert remaining["holds"] == {"halted": 1, "blocked": 0, "owner": 0, "dormant": 1}

    def test_attemptless_failed_task_older_than_a_week_is_only_dormant(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1505", state=Ticket.State.CODED
        )
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket), phase="testing", status=Task.Status.FAILED
        )
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=9))
        remaining = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))["remaining"]
        assert remaining["items"] == []
        assert remaining["holds"] == {"halted": 0, "blocked": 0, "owner": 0, "dormant": 1}

    def test_reviewer_only_work_fills_the_queue_but_lists_no_item(self, checkpoint_file: Path) -> None:
        reviewer = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1500", role=Ticket.Role.REVIEWER
        )
        PullRequest.objects.create(
            ticket=reviewer,
            overlay="acme",
            url="https://github.com/acme/widgets/pull/1501",
            repo="acme/widgets",
            iid="1501",
        )
        Task.objects.create(ticket=reviewer, session=Session.objects.create(ticket=reviewer), phase="reviewing")
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert [row["label"] for row in remaining["items"]] == ["Total", "Queue"]
        assert remaining["queue"]["pending"] == {"reviewing": 1}
        assert remaining["eta"]["basis"] == "no current work in scope"

    def test_queue_only_remaining_is_not_reported_as_nothing_in_either_scope(self, checkpoint_file: Path) -> None:
        reviewer = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1506", role=Ticket.Role.REVIEWER
        )
        Task.objects.create(ticket=reviewer, session=Session.objects.create(ticket=reviewer), phase="reviewing")
        for flags in (("--this-overlay",), ()):
            with _two_overlay_patch():
                terse = json.loads(_call("checking", "show", *flags, "--no-advance", "--json"))["terse"]
            assert not terse.startswith("Nothing since")
            assert "Remaining" in terse
            assert "Queue" in terse

    def test_human_table_shows_reviewer_only_queue(self, checkpoint_file: Path) -> None:
        reviewer = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1607", role=Ticket.Role.REVIEWER
        )
        Task.objects.create(ticket=reviewer, session=Session.objects.create(ticket=reviewer), phase="reviewing")

        for flags in (("--this-overlay",), ()):
            with _two_overlay_patch():
                out = _call("checking", "show", *flags, "--no-advance")
            assert not out.startswith("Nothing since")
            assert "Remaining" in out
            assert "Queue" in out
            assert "reviewing" in out

    def test_failed_latest_task_halts_an_open_pr(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="reviewing",
            status=Task.Status.FAILED,
        )
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error="agent crashed")
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["eta"]["basis"] == "halted"
        assert payload["remaining"]["holds"]["halted"] == 1

    def test_operator_cancelled_latest_task_is_halted_with_its_reason(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing", status=Task.Status.FAILED
        )
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error="cancelled: operator")
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["eta"]["basis"] == "halted"
        assert "cancelled by an operator" in item["detail"]
        assert "#600" not in [row["label"] for row in payload["needs_you"]["items"]]

    def test_owner_holds_only_count_live_parked_tasks_in_scope(self, checkpoint_file: Path) -> None:
        self._open_pr()
        for number, overlay, status in ((1100, "beta", Task.Status.PENDING), (1101, "acme", Task.Status.FAILED)):
            ticket = Ticket.objects.create(
                overlay=overlay,
                issue_url=f"https://github.com/{overlay}/widgets/issues/{number}",
                state=Ticket.State.CODED,
            )
            task = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="testing",
                status=status,
                not_before=timezone.now() + timedelta(hours=1),
            )
            DeferredQuestion.objects.create(question="Choose scope?", parked_task=task)
            if overlay == "acme":
                Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="testing")
        reviewer_ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/1102",
            role=Ticket.Role.REVIEWER,
        )
        reviewer_task = Task.objects.create(
            ticket=reviewer_ticket,
            session=Session.objects.create(ticket=reviewer_ticket),
            phase="reviewing",
            not_before=timezone.now() + timedelta(hours=1),
        )
        DeferredQuestion.objects.create(question="Review?", parked_task=reviewer_task)
        concurrent_ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/1103",
            state=Ticket.State.CODED,
        )
        waiting_task = Task.objects.create(
            ticket=concurrent_ticket,
            session=Session.objects.create(ticket=concurrent_ticket),
            phase="testing",
            not_before=timezone.now() + timedelta(hours=1),
        )
        DeferredQuestion.objects.create(question="Choose approach?", parked_task=waiting_task)
        Task.objects.create(
            ticket=concurrent_ticket,
            session=Session.objects.create(ticket=concurrent_ticket),
            phase="testing",
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + timedelta(hours=1),
        )
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert remaining["holds"]["owner"] == 0
        item = next(row for row in remaining["items"] if row["label"] == "#1101")
        assert "waiting on you" not in item["detail"]
        concurrent = next(row for row in remaining["items"] if row["label"] == "#1103")
        assert "waiting on you" not in concurrent["detail"]

    def test_global_seats_and_older_unadmitted_work_affect_local_eta(self, checkpoint_file: Path) -> None:
        now = timezone.now()
        for number, overlay, status in (
            (990, "beta", Task.Status.CLAIMED),
            (991, "beta", Task.Status.PENDING),
            (992, "acme", Task.Status.PENDING),
            (993, "beta", Task.Status.PENDING),
        ):
            ticket = Ticket.objects.create(
                overlay=overlay,
                issue_url=f"https://github.com/{overlay}/widgets/issues/{number}",
                short_description="queue work",
                state=Ticket.State.PLAN_RECORDED,
            )
            task = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="coding",
                status=status,
                lease_expires_at=now + timedelta(hours=1) if status == Task.Status.CLAIMED else None,
            )
            Task.objects.filter(pk=task.pk).update(created_at=now - timedelta(minutes=1000 - number))
            if number == 993:
                Task.objects.filter(pk=task.pk).update(admitted_at=now - timedelta(minutes=1))
        for index in range(5):
            ticket = Ticket.objects.create(
                overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{995 + index}"
            )
            task = Task.objects.create(
                ticket=ticket,
                session=Session.objects.create(ticket=ticket),
                phase="coding",
                status=Task.Status.COMPLETED,
            )
            ended = now - timedelta(minutes=1)
            Task.objects.filter(pk=task.pk).update(admitted_at=ended - timedelta(minutes=10))
            TaskAttempt.objects.create(task=task, ended_at=ended, exit_code=0)
        widths = MagicMock(expensive=1, review=1, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        queue = payload["remaining"]["queue"]
        assert queue["claimed"] == {"coding": 1}
        assert queue["pending"] == {"coding": 2}
        assert queue["oldest_pending_phase"] == "coding"
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "#992")
        assert item["eta"]["median_minutes"] == 100

    def test_review_task_uses_its_own_phase_history(self, checkpoint_file: Path) -> None:
        ticket = self._open_pr()
        Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="codex_reviewing")
        now = timezone.now()
        for phase, minutes in (("codex_reviewing", 10), ("reviewing", 30)):
            for index in range(5):
                sample_ticket = Ticket.objects.create(
                    overlay="acme", issue_url=f"https://github.com/acme/widgets/issues/{phase}-{index}"
                )
                sample = Task.objects.create(
                    ticket=sample_ticket,
                    session=Session.objects.create(ticket=sample_ticket),
                    phase=phase,
                    status=Task.Status.COMPLETED,
                )
                ended = now - timedelta(minutes=1)
                Task.objects.filter(pk=sample.pk).update(admitted_at=ended - timedelta(minutes=minutes))
                TaskAttempt.objects.create(task=sample, ended_at=ended, exit_code=0)
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "acme/widgets#601")
        assert item["steps"][0] == "factory review"
        assert any(row["step"] == "codex_reviewing" for row in payload["remaining"]["durations"])
        assert item["eta"]["median_minutes"] == 100

    def test_merged_ticket_with_open_task_still_shows_deploy(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/1200",
            state=Ticket.State.MERGED,
        )
        Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        item = next(row for row in payload["remaining"]["items"] if row["label"] == "#1200")
        assert item["steps"] == ["deploy"]
        assert "then" not in item["detail"]

    def test_held_work_is_listed_after_active_work(self, checkpoint_file: Path) -> None:
        active = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1300", state=Ticket.State.CODED
        )
        Task.objects.create(ticket=active, session=Session.objects.create(ticket=active), phase="testing")
        halted = Ticket.objects.create(
            overlay="acme", issue_url="https://github.com/acme/widgets/issues/1301", state=Ticket.State.CODED
        )
        failed = Task.objects.create(
            ticket=halted, session=Session.objects.create(ticket=halted), phase="testing", status=Task.Status.FAILED
        )
        TaskAttempt.objects.create(task=failed, ended_at=timezone.now(), exit_code=1, error="agent crashed")
        self._open_pr()
        payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
        assert [row["label"] for row in payload["remaining"]["items"][2:]] == ["acme/widgets#601", "#1300", "#1301"]

    def test_remaining_read_has_no_sql_writes(self, checkpoint_file: Path) -> None:
        self._open_pr()
        with CaptureQueriesContext(connection) as queries:
            _call("checking", "show", "--this-overlay", "--no-advance", "--json")
        assert not [
            query["sql"]
            for query in queries
            if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]

    def test_cap_links_and_all_overlay_tags(self, checkpoint_file: Path) -> None:
        for index in range(6):
            overlay = "acme" if index < 3 else "beta"
            ticket = Ticket.objects.create(
                overlay=overlay,
                issue_url=f"https://github.com/{overlay}/widgets/issues/{920 + index}",
                short_description=f"item {index}",
                state=Ticket.State.PR_OPENED,
            )
            PullRequest.objects.create(
                ticket=ticket,
                overlay=overlay,
                repo=f"{overlay}/widgets",
                iid=str(930 + index),
                url=f"https://github.com/{overlay}/widgets/pull/{930 + index}",
            )
        with _two_overlay_patch():
            payload = json.loads(_call("checking", "show", "--no-advance", "--json"))
        remaining = payload["remaining"]
        assert remaining["total"] == 6
        assert len(remaining["items"]) == 7
        assert all(row["detail"] in payload["terse"] for row in remaining["items"])
        assert "…and 1 more" in payload["terse"]
        assert "[beta]" in payload["terse"]
        assert "[beta/widgets#935 (item 5)](https://github.com/beta/widgets/pull/935)" in payload["terse"]

    def test_pr_query_count_does_not_grow_with_pr_count(self, checkpoint_file: Path) -> None:
        def add_prs(start: int, stop: int) -> None:
            for index in range(start, stop):
                ticket = Ticket.objects.create(
                    overlay="acme",
                    issue_url=f"https://github.com/acme/widgets/issues/{950 + index}",
                    short_description=f"item {index}",
                    state=Ticket.State.PR_OPENED,
                )
                PullRequest.objects.create(
                    ticket=ticket,
                    overlay="acme",
                    repo="acme/widgets",
                    iid=str(960 + index),
                    url=f"https://github.com/acme/widgets/pull/{960 + index}",
                )

        def query_count() -> tuple[int, int]:
            with CaptureQueriesContext(connection) as queries:
                payload = json.loads(_call("checking", "show", "--this-overlay", "--no-advance", "--json"))
            return len(queries), payload["remaining"]["total"]

        add_prs(0, 3)
        widths = MagicMock(expensive=2, review=1, retiring=1)
        with patch("teatree.core.checking_remaining.headless_lane_widths", return_value=widths):
            first, first_total = query_count()
            add_prs(3, 9)
            second, second_total = query_count()
        assert (first_total, second_total) == (3, 9)
        assert second == first


class TestCheckingNotify:
    """``--notify`` routes the recap through the real ``notify_user`` egress.

    The recap is an ``OWNER_DELIVERY`` status signal, so since #4524 it reaches
    Slack only if its key is registered as worth a push — unregistered, it is
    recorded on the pulled surface instead (never DM'd). ``checking_recap`` is
    not registered (it is exactly the "status the owner can read on demand"
    class #4524 exists to keep off the DM), so these tests assert against the
    pulled ``BotPing`` row rather than a live Slack post.
    """

    def test_notify_records_the_table_and_fence_on_the_pulled_surface(self, checkpoint_file: Path) -> None:
        # Anti-vacuity: the pulled row must be produced by the real ``notify_user``
        # egress (mocked Slack backend), not a direct formatter call — the
        # rendered fence must reach the ``BotPing`` row, and no Slack post fires.
        _merged_ticket()
        backend = _notify_backend()
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
        ):
            _call("checking", "show", "--this-overlay", "--notify")
        backend.open_dm.assert_not_called()
        backend.post_message.assert_not_called()
        row = BotPing.objects.get(idempotency_key__startswith="checking_recap:acme:")
        assert row.status == BotPing.Status.PULLED
        assert row.audience == NotifyAudience.OWNER_DELIVERY.value
        assert "```" in row.text
        assert "acme/widgets#7" in row.text

    def test_a_metered_attempt_reaches_the_table_and_the_recap(self, checkpoint_file: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme/widgets/issues/5399",
            short_description="paid fall-through",
        )
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        attempt = TaskAttempt.objects.create(
            task=task, lane=TaskAttempt.Lane.METERED, route_candidate_index=1, cost_usd=1.25, exit_code=0
        )
        TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=timezone.now() - timedelta(minutes=5))
        backend = _notify_backend()
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
        ):
            out = _call("checking", "show", "--this-overlay", "--no-advance", "--notify")
        assert "Metered" in out
        assert "estimated $1.25" in out
        row = BotPing.objects.get(idempotency_key__startswith="checking_recap:acme:")
        assert "Metered" in row.text
        assert "estimated $1.25" in row.text

    def test_notify_skipped_when_nothing_to_report(self, checkpoint_file: Path) -> None:
        backend = _notify_backend()
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
        ):
            _call("checking", "show", "--this-overlay", "--notify")
        backend.post_message.assert_not_called()

    def test_an_undelivered_recap_leaves_the_window_unadvanced(self, checkpoint_file: Path) -> None:
        """The checkpoint consumes the window — advancing it over a lost DM loses those rows."""
        _merged_ticket()
        before = load_checkpoint(checkpoint_file)
        # Patched at the delivery seam, not at a transport: the recap is a PULLED row now,
        # so a post_message stub proves nothing about whether the recap landed.
        with patch("teatree.core.management.commands.checking.notify_user", return_value=False):
            _call("checking", "show", "--this-overlay", "--notify")
        assert load_checkpoint(checkpoint_file) == before

    def test_unchanged_recap_deduped_to_one_pulled_row(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        backend = _notify_backend()
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
        ):
            _call("checking", "show", "--this-overlay", "--no-advance", "--notify")
            _call("checking", "show", "--this-overlay", "--no-advance", "--notify")
        backend.post_message.assert_not_called()
        assert BotPing.objects.filter(idempotency_key__startswith="checking_recap:acme:").count() == 1


class TestCheckingShowAllOverlays:
    """``checking show`` default = all overlays; ``--this-overlay`` = current overlay only."""

    def _merged_ticket_for(self, overlay: str, *, number: int, pr_id: int, slug: str) -> Ticket:
        ticket = Ticket.objects.create(
            overlay=overlay,
            issue_url=f"https://github.com/{slug}/issues/{number}",
            state=Ticket.State.REVIEW_REQUESTED,
            short_description=f"{overlay} work",
        )
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                ticket=ticket,
            ),
        )
        MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        return ticket

    def test_all_overlays_aggregation(self, tmp_path: Path) -> None:
        """Default path reports merged tickets from both overlays."""
        self._merged_ticket_for("acme", number=1, pr_id=10, slug="acme/widgets")
        self._merged_ticket_for("beta", number=2, pr_id=20, slug="beta/core")

        with _two_overlay_patch():
            out = _call("checking", "show", "--no-advance")
        assert "acme/widgets#10" in out
        assert "beta/core#20" in out

    def test_this_overlay_flag_scopes_to_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--this-overlay`` shows only the active overlay; the other's checkpoint is untouched."""
        self._merged_ticket_for("acme", number=1, pr_id=10, slug="acme/widgets")
        self._merged_ticket_for("beta", number=2, pr_id=20, slug="beta/core")

        acme_ckpt = tmp_path / "acme_ckpt.json"
        beta_ckpt = tmp_path / "beta_ckpt.json"

        def _fake_checkpoint_path(*, overlay: str | None = None) -> Path:
            import os  # noqa: PLC0415

            name = overlay if overlay is not None else os.environ.get("T3_OVERLAY_NAME", "")
            if name == "acme":
                return acme_ckpt
            if name == "beta":
                return beta_ckpt
            return tmp_path / f"checking_checkpoint_{name or 'global'}.json"

        monkeypatch.setattr("teatree.core.checkpoint.checkpoint_path", _fake_checkpoint_path)
        monkeypatch.setattr("teatree.core.management.commands.checking.checkpoint_path", _fake_checkpoint_path)

        with _two_overlay_patch():
            out = _call("checking", "show", "--this-overlay")

        assert "acme/widgets#10" in out
        assert "beta/core#20" not in out
        assert not beta_ckpt.exists()

    def test_per_overlay_marker_independence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Advancing one overlay's marker must not affect the other's checkpoint file."""
        acme_ckpt = tmp_path / "acme_ckpt.json"
        beta_ckpt = tmp_path / "beta_ckpt.json"

        def _fake_checkpoint_path(*, overlay: str | None = None) -> Path:
            import os  # noqa: PLC0415

            name = overlay if overlay is not None else os.environ.get("T3_OVERLAY_NAME", "")
            if name == "acme":
                return acme_ckpt
            if name == "beta":
                return beta_ckpt
            return tmp_path / f"checking_checkpoint_{name or 'global'}.json"

        monkeypatch.setattr("teatree.core.checkpoint.checkpoint_path", _fake_checkpoint_path)
        monkeypatch.setattr("teatree.core.management.commands.checking.checkpoint_path", _fake_checkpoint_path)

        with _two_overlay_patch():
            _call("checking", "show")

        assert acme_ckpt.exists()
        assert beta_ckpt.exists()
        acme_ts = load_checkpoint(acme_ckpt)
        beta_ts = load_checkpoint(beta_ckpt)
        assert acme_ts is not None
        assert beta_ts is not None

    def test_all_overlays_json_has_all_overlays_shape(self, tmp_path: Path) -> None:
        """``--json`` on the all-overlays path returns the AllOverlaysReportDict shape."""
        self._merged_ticket_for("acme", number=1, pr_id=10, slug="acme/widgets")

        with _two_overlay_patch():
            out = _call("checking", "show", "--no-advance", "--json")

        payload = json.loads(out)
        assert "all_overlays" in payload
        assert "merged" in payload

    def test_all_overlays_notify_recap_carries_each_overlays_metered_table(self, tmp_path: Path) -> None:
        for overlay, number in (("acme", 1), ("beta", 2)):
            ticket = Ticket.objects.create(
                overlay=overlay, issue_url=f"https://github.com/{overlay}/core/issues/{number}"
            )
            task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
            attempt = TaskAttempt.objects.create(
                task=task, lane=TaskAttempt.Lane.METERED, route_candidate_index=1, cost_usd=1.25, exit_code=0
            )
            TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=timezone.now() - timedelta(minutes=5))
        backend = _notify_backend()
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
            _two_overlay_patch(),
        ):
            _call("checking", "show", "--no-advance", "--notify")
        row = BotPing.objects.get(idempotency_key__startswith="checking_recap:all:")
        assert "Metered (2)" in row.text
        assert row.text.count("estimated $1.25") == 2


class TestMachineOutputChannel:
    """``checking show`` is a seam verb: stdout carries JSON or nothing at all."""

    @staticmethod
    def _channels(*args: str) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command("checking", "show", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_human_mode_leaves_stdout_empty(self, checkpoint_file: Path) -> None:
        out, err = self._channels("--this-overlay")
        assert out == ""
        assert "Nothing since " in err

    def test_json_mode_puts_only_json_on_stdout(self, checkpoint_file: Path) -> None:
        out, err = self._channels("--this-overlay", "--json")
        assert "since" in json.loads(out)
        assert err == ""
