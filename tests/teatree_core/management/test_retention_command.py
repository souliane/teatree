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
from contextlib import AbstractContextManager
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup import artifact_eviction, artifact_removal, process_table
from teatree.core.management.commands.retention import Command, RetentionReport, _vacuum_row
from teatree.core.models import IncomingEvent, Session, Task, TaskAttempt, Ticket
from teatree.utils.django_db.vacuum import VacuumOutcome
from tests._git_repo import make_git_repo
from tests._process_table_venue import blinded_process_table, this_process_in
from tests._procfs import pinned_venue_proc

_REGISTRY = "teatree.core.cleanup.checkout_registry"

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
        # Without this the sweep reads the machine's live /proc, so the verdict is a
        # property of whatever else runs: green in a container, red on a systemd host.
        self.enterContext(pinned_venue_proc())
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

    def test_a_dry_run_on_a_sighted_probe_reports_no_refusal(self) -> None:
        payload = self._scratch_json()

        assert payload["refused"] is False


class ScratchSweepRefusalCommandTests(TestCase):
    """An unsighted probe exits NON-ZERO on --apply rather than reporting a clean no-op."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        stale = self.root / "t3db.sqlite3"
        stale.write_bytes(b"x" * 2048)
        old = timezone.now().timestamp() - 9 * 86400
        os.utime(stale, (old, old))
        self.stale = stale
        # No pinned table: the autouse conftest fixture points the venue probe at a
        # path that is not a process table, which is exactly the unsighted case.

    def _run(self, *args: str) -> dict[str, Any]:
        out = StringIO()
        call_command("retention", "scratch", "--root", str(self.root), "--days", "3", *args, "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_a_dry_run_reports_the_refusal_without_failing(self) -> None:
        payload = self._run()

        assert payload["refused"] is True
        assert payload["candidate_bytes"] == 0
        assert "unsighted" in payload["probe_gap"]

    def test_apply_exits_non_zero_and_still_writes_the_payload(self) -> None:
        out = StringIO()

        with pytest.raises(SystemExit) as exit_info:
            call_command(
                "retention", "scratch", "--root", str(self.root), "--days", "3", "--apply", "--json", stdout=out
            )

        assert exit_info.value.code == 1
        assert json.loads(out.getvalue())["refused"] is True
        assert self.stale.exists()

    def test_the_human_view_names_the_refusal_and_the_arming_precondition(self) -> None:
        err = StringIO()

        call_command("retention", "scratch", "--root", str(self.root), "--days", "3", stderr=err)

        rendered = err.getvalue()
        assert "REFUSED" in rendered
        assert "ptrace" in rendered


class UnlistableScratchRootCommandTests(TestCase):
    """A root the sweep could not read into exits NON-ZERO on --apply, like an unsighted probe.

    The venue probe is PINNED to an answering table throughout: the autouse fixture
    leaves it unsighted, whose gap also says "not listable" — about the process table
    rather than the root — so an unpinned arm would pass on the wrong refusal.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())) / "not-a-directory"
        self.root.write_bytes(b"")
        self.enterContext(pinned_venue_proc())

    def _run(self, *args: str) -> dict[str, Any]:
        out = StringIO()
        call_command("retention", "scratch", "--root", str(self.root), "--days", "3", *args, "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_apply_exits_non_zero_and_still_writes_the_payload(self) -> None:
        out = StringIO()

        with pytest.raises(SystemExit) as exit_info:
            call_command(
                "retention", "scratch", "--root", str(self.root), "--days", "3", "--apply", "--json", stdout=out
            )

        assert exit_info.value.code == 1
        payload = json.loads(out.getvalue())
        assert payload["refused"] is True
        assert str(self.root) in payload["probe_gap"]

    def test_a_dry_run_reports_the_refusal_without_failing(self) -> None:
        payload = self._run()

        assert payload["refused"] is True
        assert payload["resident_bytes"] == 0
        assert str(self.root) in payload["probe_gap"]

    def test_a_listable_root_is_the_sighted_positive_control(self) -> None:
        listable = self.root.parent / "listable"
        listable.mkdir()

        payload = self._run("--root", str(listable))

        assert payload["refused"] is False
        assert payload["probe_gap"] == ""


class ArtifactEvictionCommandTests(TestCase):
    """The operator's on-demand view of the same plan the autonomous pass computes (#4244).

    It gates nothing — the sweep runs unconditionally and deletes only what it has PROVED
    reconstructible. What this surface owes is a HONEST account: an artifact the guards
    kept, a deferral, and a deletion that failed part-way must each read as themselves, on
    a host whose overlay points every worktree's ``node_modules`` at one clone directory.
    """

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.workspace = tmp_path
        self.host_proc = tmp_path / "host-proc"
        (self.host_proc / "1").mkdir(parents=True)
        (self.host_proc / "1" / "cwd").symlink_to(tmp_path / "elsewhere")
        (self.host_proc / "1" / "exe").symlink_to(tmp_path / "elsewhere" / "bin" / "process")
        this_process_in(self.host_proc)
        self.enterContext(patch.object(process_table, "_HOST_PROC_ROOT", self.host_proc))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(tmp_path,)))
        self.enterContext(patch(f"{_REGISTRY}.Path.cwd", return_value=tmp_path / "nowhere"))
        self.enterContext(patch(f"{_COMMAND}.worktree_root", return_value=tmp_path))

    def _dormant_artifact(self, checkout_name: str = "clone", *, size: int = 4096) -> Path:
        checkout = make_git_repo(self.workspace / checkout_name)
        artifact = checkout / "node_modules"
        (artifact / "lib").mkdir(parents=True)
        (artifact / "lib" / "big.so").write_bytes(b"x" * size)
        (checkout / "package-lock.json").touch()
        for path in (artifact, checkout):
            os.utime(path, (1_600_000_000, 1_600_000_000))
        return artifact

    def _run(self, *args: str) -> dict[str, Any]:
        out = StringIO()
        call_command("retention", "artifacts", *args, "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_the_default_is_a_dry_run_that_deletes_nothing(self) -> None:
        artifact = self._dormant_artifact()

        payload = self._run("--days", "1")

        assert artifact.exists(), "a dry run may not delete"
        assert payload["applied"] is False
        assert payload["freed_bytes"] == 0
        assert payload["estimated_bytes"] > 0
        assert [row["path"] for row in payload["entries"] if row["verdict"] == "EVICT"] == [str(artifact)]

    def test_apply_actually_evicts(self) -> None:
        """Anti-vacuity: the dry run's restraint must be the flag, not a broken pass."""
        artifact = self._dormant_artifact()

        payload = self._run("--days", "1", "--apply")

        assert not artifact.exists()
        assert payload["applied"] is True
        assert payload["freed_bytes"] > 0

    def test_a_symlinked_artifact_and_its_target_both_read_as_kept(self) -> None:
        """The check the plan's §6 step 2 prescribes before any deletion is allowed."""
        artifact = self._dormant_artifact()
        worktree = make_git_repo(self.workspace / "wt-1234")
        link = worktree / "node_modules"
        link.symlink_to(artifact)
        os.utime(worktree, (1_600_000_000, 1_600_000_000))

        payload = self._run("--days", "1")

        kept = {row["path"] for row in payload["entries"] if row["verdict"] == "KEEP"}
        assert str(link) in kept, "the link must appear under KEEP"
        assert str(artifact) in kept, "so must the target every worktree depends on"
        assert [row["path"] for row in payload["entries"] if row["verdict"] == "EVICT"] == []

    def test_a_refused_pass_reports_the_reason_and_exits_non_zero(self) -> None:
        """A typer.Exit here would exit 0 under call_command and CI would report green."""
        out = StringIO()
        with blinded_process_table(self.workspace / "gone"), pytest.raises(SystemExit) as exit_info:
            call_command("retention", "artifacts", "--days", "1", "--json", stdout=out)

        assert exit_info.value.code == 1
        payload = json.loads(out.getvalue())
        assert payload["refused"] is True
        assert payload["refusal"], "the payload is written BEFORE the raise, or the reason is lost"

    def test_a_delete_that_failed_part_way_reads_as_stopped_not_as_an_untouched_keep(self) -> None:
        """The state ``_failed_delete_state`` exists to name never reached the surface (F10).

        Rendered as ``KEEP — dormant, rebuildable``, a part-removed tree reads as "nothing
        happened", and the operator is never told it must be rebuilt before it is used.
        """
        artifact = self._dormant_artifact()

        with patch.object(artifact_removal.shutil, "rmtree", side_effect=OSError("Permission denied")):
            payload = self._run("--days", "1", "--apply")

        row = next(row for row in payload["entries"] if row["path"] == str(artifact))
        assert row["verdict"] == "STOPPED", payload["entries"]
        assert "must be rebuilt before use" in row["reason"], row
        assert payload["freed_bytes"] == 0

    def _blind_after_first_delete(
        self,
    ) -> tuple[Path, Path, AbstractContextManager[object], AbstractContextManager[object]]:
        first = self._dormant_artifact("aaa-first", size=100_000)
        second = self._dormant_artifact("zzz-second", size=1_000)
        answering = self.host_proc / "1" / "cwd"
        readlink = Path.readlink
        unreadable = PermissionError()
        blinded = False

        def _readlink(path: Path) -> Path:
            if blinded and path == answering:
                raise unreadable
            return readlink(path)

        real_remove = artifact_eviction._remove_anchored_candidate
        removed = 0

        def _remove(candidate: artifact_eviction.ArtifactCandidate) -> str:
            nonlocal removed, blinded
            reason = real_remove(candidate)
            if not reason:
                removed += 1
            if removed == 1:
                blinded = True  # the only pid that spoke stops speaking: a blind table, not a gap
            return reason

        return (
            first,
            second,
            patch.object(Path, "readlink", _readlink),
            patch.object(
                artifact_eviction,
                "_remove_anchored_candidate",
                side_effect=_remove,
            ),
        )

    def test_a_mid_batch_refusal_reports_the_removed_and_stopped_candidates(self) -> None:
        first, second, readlink_patch, rmtree_patch = self._blind_after_first_delete()
        out = StringIO()

        with readlink_patch, rmtree_patch, pytest.raises(SystemExit) as exit_info:
            call_command("retention", "artifacts", "--days", "1", "--apply", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert exit_info.value.code == 1
        assert not first.exists()
        assert second.exists()
        assert payload["evicted_count"] == 1
        second_row = next(row for row in payload["entries"] if row["path"] == str(second))
        assert second_row["verdict"] == "STOPPED"

    def test_a_mid_batch_refusal_human_summary_does_not_claim_nothing_was_removed(self) -> None:
        _first, _second, readlink_patch, rmtree_patch = self._blind_after_first_delete()
        out = StringIO()

        with readlink_patch, rmtree_patch, pytest.raises(SystemExit):
            call_command("retention", "artifacts", "--days", "1", "--apply", stderr=out)

        rendered = out.getvalue()
        assert "nothing was removed" not in rendered
        assert "after evicting 1 artifact" in rendered
