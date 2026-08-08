"""``t3 <overlay> retention prune`` — the operator surface for #3693.

Integration-first via ``call_command`` against the real DB: the default is a dry
run that deletes nothing and reports the plan; ``--apply`` deletes only the
terminal-owned rows past the window. ``--json`` round-trips the machine payload.

``--apply`` also VACUUMs (#3852) — deleting rows on SQLite reclaims no disk on its
own, so a prune that only drops rows leaves the file exactly as large. The vacuum
itself is exercised against a real file in ``tests/teatree_utils/django_db/test_vacuum.py``;
here it is stubbed so the wiring (called on apply, never on a dry run, reported
either way) is what is under test.
"""

import datetime as dt
import json
import os
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.management.commands.retention import Command, RetentionReport, _vacuum_row
from teatree.core.models import IncomingEvent, Session, Task, TaskAttempt, Ticket
from teatree.utils.django_db.vacuum import VacuumOutcome

_OLD = timezone.now() - dt.timedelta(days=60)
_COMMAND = "teatree.core.management.commands.retention"
_PAGE_SIZE = 4096
_RECLAIMED = VacuumOutcome(
    ran=True,
    reason="rebuilt",
    file_bytes_before=_PAGE_SIZE * 293,
    file_bytes_after=_PAGE_SIZE * 146,
    page_size=_PAGE_SIZE,
    pages_before=293,
    pages_after=146,
    free_pages_before=147,
    free_pages_after=0,
)
#: The rebuild landed but a live reader deferred the truncation, so the file still
#: reads at its pre-vacuum size — the #3979 shape that reported 0.0 MiB.
_RECLAIMED_FILE_LAGGING = replace(_RECLAIMED, file_bytes_after=_PAGE_SIZE * 293)


def _prune_json(outcome: VacuumOutcome, *args: str) -> tuple[dict[str, Any], MagicMock]:
    out = StringIO()
    with patch(f"{_COMMAND}.vacuum_control_db", return_value=outcome) as vacuum:
        call_command("retention", "prune", *args, "--json", stdout=out)
    return json.loads(out.getvalue()), vacuum


def _old_terminal_attempt() -> TaskAttempt:
    ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.MERGED)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
    attempt = TaskAttempt.objects.create(task=task)
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=_OLD)
    return attempt


def _old_processed_event(key: str) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        idempotency_key=key,
        received_at=_OLD,
        processed_at=timezone.now(),
    )


class RetentionCommandStructureTestCase(TestCase):
    def test_command_exposes_prune(self) -> None:
        assert callable(Command.prune)

    def test_report_payload_keys(self) -> None:
        report: RetentionReport = {
            "applied": False,
            "total_rows": 0,
            "tables": [],
            "vacuum": _vacuum_row(VacuumOutcome(ran=False, reason="dry run")),
        }
        assert set(report) == {"applied", "total_rows", "tables", "vacuum"}


class RetentionPruneCommandTestCase(TestCase):
    def test_dry_run_reports_but_deletes_nothing(self) -> None:
        _old_terminal_attempt()
        _old_processed_event("k1")
        err = StringIO()
        call_command("retention", "prune", stderr=err)
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1
        # The human view routes to stderr (machine JSON owns stdout).
        assert "dry run" in err.getvalue().lower()

    def test_apply_deletes_prunable_rows(self) -> None:
        _old_terminal_attempt()
        _old_processed_event("k1")
        call_command("retention", "prune", "--apply")
        assert TaskAttempt.objects.count() == 0
        assert IncomingEvent.objects.count() == 0

    def test_apply_spares_live_ticket_rows(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        live = TaskAttempt.objects.create(task=task)
        TaskAttempt.objects.filter(pk=live.pk).update(started_at=_OLD)
        call_command("retention", "prune", "--apply")
        assert TaskAttempt.objects.filter(pk=live.pk).exists()

    def test_json_payload_round_trips(self) -> None:
        _old_terminal_attempt()
        out = StringIO()
        call_command("retention", "prune", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        assert payload["applied"] is False
        assert payload["total_rows"] == 1
        tables = {row["table"]: row for row in payload["tables"]}
        assert tables["TaskAttempt"]["rows"] == 1
        assert tables["TaskAttempt"]["retention_days"] == 30


class RetentionPruneVacuumTestCase(TestCase):
    """Rows are only half a prune — the freed pages must reach the filesystem (#3852)."""

    def test_apply_vacuums_and_reports_the_page_derived_reclaim(self) -> None:
        payload, vacuum = _prune_json(_RECLAIMED, "--apply")

        vacuum.assert_called_once_with()
        assert payload["vacuum"] == {
            "ran": True,
            "reason": "rebuilt",
            "summary": _RECLAIMED.summary,
            "bytes_reclaimed": _PAGE_SIZE * 147,
            "page_size": _PAGE_SIZE,
            "pages_before": 293,
            "pages_after": 146,
            "free_pages_before": 147,
            "free_pages_after": 0,
            "file_bytes_before": _PAGE_SIZE * 293,
            "file_bytes_after": _PAGE_SIZE * 146,
            "file_caught_up": True,
        }

    def test_a_reclaim_the_file_has_not_applied_yet_is_still_reported(self) -> None:
        """The #3979 shape: the rebuild landed, the truncation is still pending."""
        payload, _ = _prune_json(_RECLAIMED_FILE_LAGGING, "--apply")

        assert payload["vacuum"]["bytes_reclaimed"] == _PAGE_SIZE * 147
        assert payload["vacuum"]["file_caught_up"] is False
        assert "next checkpoint" in payload["vacuum"]["summary"]

    def test_the_human_row_carries_the_page_and_freelist_deltas(self) -> None:
        """The direct evidence the rebuild happened, and it does not depend on a stat."""
        err = StringIO()
        with patch(f"{_COMMAND}.vacuum_control_db", return_value=_RECLAIMED):
            call_command("retention", "prune", "--apply", stderr=err)

        rendered = err.getvalue()
        assert "pages 293" in rendered
        assert "free 147" in rendered

    def test_dry_run_never_vacuums(self) -> None:
        """VACUUM rewrites the whole file — a preview that mutates 1.12 GB is not a preview."""
        payload, vacuum = _prune_json(_RECLAIMED)

        vacuum.assert_not_called()
        assert payload["vacuum"]["ran"] is False

    def test_a_vacuum_that_could_not_run_reports_its_reason(self) -> None:
        """A stated reason is what separates "nothing to reclaim" from "never happened"."""
        blocked = VacuumOutcome(ran=False, reason="postgresql is not SQLite — VACUUM is not applicable")

        payload, _ = _prune_json(blocked, "--apply")

        assert payload["vacuum"]["ran"] is False
        assert "not applicable" in payload["vacuum"]["reason"]
        assert payload["vacuum"]["bytes_reclaimed"] == 0
        assert payload["vacuum"]["summary"] == payload["vacuum"]["reason"]


class ScratchSweepCommandTests(TestCase):
    """``t3 <overlay> retention scratch`` — dry-run default, size-ranked report, real reclaim."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        stale = self.root / "t3db.sqlite3"
        stale.write_bytes(b"x" * 2048)
        old = timezone.now().timestamp() - 9 * 86400
        os.utime(stale, (old, old))
        self.stale = stale

    def _scratch_json(self, *args: str) -> dict[str, Any]:
        out = StringIO()
        call_command("retention", "scratch", "--root", str(self.root), "--days", "3", *args, "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_reports_the_reclaimable_bytes_without_touching_anything(self) -> None:
        payload = self._scratch_json()

        assert payload["applied"] is False
        assert payload["candidate_bytes"] == 2048
        assert payload["reclaimed_bytes"] == 0
        assert [entry["path"] for entry in payload["entries"]] == [str(self.stale)]
        assert self.stale.exists()

    def test_apply_reclaims_the_stale_scratch_and_reports_what_it_freed(self) -> None:
        payload = self._scratch_json("--apply")

        assert payload["applied"] is True
        assert payload["reclaimed_bytes"] == 2048
        assert not self.stale.exists()

    def test_the_configured_retention_window_is_the_default(self) -> None:
        with patch(f"{_COMMAND}.get_effective_settings") as settings:
            settings.return_value.scratch_sweep_root = str(self.root)
            settings.return_value.scratch_retention_days = 90
            out = StringIO()
            call_command("retention", "scratch", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["retention_days"] == 90
        assert payload["candidate_bytes"] == 0

    def test_the_human_view_names_every_kept_entry_and_its_reason(self) -> None:
        fresh = self.root / "claude-statusline"
        fresh.mkdir()
        err = StringIO()

        call_command("retention", "scratch", "--root", str(self.root), "--days", "3", stderr=err)

        rendered = err.getvalue()
        assert "dry run" in rendered
        assert "protected path" in rendered
        assert "2.0KiB" in rendered
