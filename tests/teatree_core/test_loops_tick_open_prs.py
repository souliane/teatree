"""Every tick reports every open PR it observed, with age and next task, started and oldest first."""

import io
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import Loop, PlanArtifact, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.core.review.pr_inventory import OpenPrRow, PrInventoryEntry, write_inventory
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.pr_stall import repair_stalled_prs
from teatree.loop.scanners.base import ScanSignal
from teatree.loops.base import MiniLoop
from tests.factories import PullRequestFactory, TicketFactory

_LOOP = "ship"


def _entry(number: int, *, age: timedelta, next_task: str, repo: str = "souliane/teatree") -> PrInventoryEntry:
    created = (timezone.now() - age).isoformat()
    return PrInventoryEntry(
        url=f"https://github.com/{repo}/pull/{number}",
        repo=repo,
        iid=number,
        title=f"PR {number}",
        created_at=created,
        updated_at=created,
        next_task=next_task,
        decision="skip",
        ci="unknown",
        approvals="unknown",
        threads="unknown",
        draft=False,
        head_sha="a" * 40,
    )


class _InventoryScanner:
    name = "inventory_stub"

    def __init__(self, overlay: str, entries: list[PrInventoryEntry]) -> None:
        self.overlay = overlay
        self.entries = entries

    def scan(self) -> list[ScanSignal]:
        write_inventory(self.overlay, self.entries)
        return []


@contextmanager
def _tick(scanners: list[_InventoryScanner]) -> Generator[Path]:
    stub = MiniLoop(
        name=_LOOP,
        default_cadence_seconds=60,
        build_jobs=lambda **_: [_ScannerJob(scanner=scanner, overlay="") for scanner in scanners],
    )
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch("teatree.loops.loop_table.iter_loops", return_value=(stub,)),
        patch("teatree.core.management.commands.loops_tick.iter_overlay_backends", return_value=[]),
        patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
        patch("teatree.core.review.pr_inventory.OverlayConfigResolver.all_names", return_value=["teatree"]),
        patch.dict(os.environ, {"T3_LOOP_SESSION_ID": "the-only-session", "T3_DATA_DIR": str(Path(tmp) / "data")}),
    ):
        yield Path(tmp) / "statusline.txt"


def _run(scanners: list[_InventoryScanner], *, json_output: bool = False) -> str:
    out, err = io.StringIO(), io.StringIO()
    with _tick(scanners) as statusline:
        call_command(
            "loops_tick",
            loop=_LOOP,
            statusline_file=statusline,
            json_output=json_output,
            stdout=out,
            stderr=err,
        )
    return out.getvalue() + err.getvalue()


class TestTickReportsOpenPrs(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.update_or_create(
            name=_LOOP,
            defaults={"enabled": True, "delay_seconds": 60, "script": f"src/teatree/loops/{_LOOP}/loop.py"},
        )

    def test_every_observed_pr_is_listed_with_age_and_next_task_started_then_oldest_first(self) -> None:
        newest = _entry(1, age=timedelta(hours=2, minutes=5), next_task="Fix failing CI")
        oldest = _entry(3, age=timedelta(days=3, minutes=5), next_task="Resolve merge conflict", repo="souliane/zeta")
        started = _entry(2, age=timedelta(days=1, minutes=5), next_task="Run independent review")
        PullRequestFactory(url=started.url, repo=started.repo, iid=started.iid)

        output = _run([_InventoryScanner("teatree", [newest, oldest, started])])

        lines = [line for line in output.splitlines() if line.startswith("OPEN")]
        assert [line.split()[1] for line in lines] == ["souliane/teatree!2", "souliane/zeta!3", "souliane/teatree!1"]
        assert "1 d" in lines[0]
        assert "Run independent review" in lines[0]
        assert "3 d" in lines[1]
        assert "Resolve merge conflict" in lines[1]
        assert "2 h" in lines[2]
        assert "Fix failing CI" in lines[2]

    def test_tick_lists_twenty_five_open_prs_with_a_next_task(self) -> None:
        entries = [
            _entry(number, age=timedelta(days=number), next_task="Run independent review") for number in range(1, 26)
        ]

        output = _run([_InventoryScanner("teatree", entries)])

        lines = [line for line in output.splitlines() if line.startswith("OPEN")]
        assert len(lines) == 25
        assert all("Run independent review" in line for line in lines)

    def test_the_structured_report_carries_the_same_rows(self) -> None:
        entry = replace(
            _entry(7, age=timedelta(days=2, minutes=5), next_task="Address requested changes"),
            reason="changes_requested",
        )

        payload = json.loads(_run([_InventoryScanner("teatree", [entry])], json_output=True))

        assert payload["open_prs"] == [
            {
                "ref": "souliane/teatree!7",
                "url": entry.url,
                "age": "2 d",
                "next_task": "Address requested changes",
                "started": False,
                "stall": "Address requested changes",
            }
        ]

    def test_a_pr_seen_by_two_scanners_is_listed_once(self) -> None:
        entry = _entry(9, age=timedelta(hours=5, minutes=5), next_task="Fix failing CI")

        output = _run([_InventoryScanner("own:teatree:abc", [entry]), _InventoryScanner("teatree", [entry])])

        assert output.count("souliane/teatree!9") == 1

    def test_a_tick_that_observed_nothing_lists_no_stale_snapshot_from_an_earlier_tick(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with _tick([]) as statusline:
            write_inventory("teatree", [_entry(4, age=timedelta(days=1), next_task="Fix failing CI")])
            call_command("loops_tick", loop=_LOOP, statusline_file=statusline, stdout=out, stderr=err)

        report = out.getvalue() + err.getvalue()
        assert "ran   loop" in report, report
        assert "souliane/teatree!4" not in report, report

    def test_an_unreadable_inventory_is_named_in_the_report_not_read_as_no_open_prs(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with _tick([]) as statusline:
            write_inventory("teatree", [_entry(5, age=timedelta(days=1), next_task="Fix failing CI")])
            next(Path(os.environ["T3_DATA_DIR"]).glob("pr-inventory/*.json")).write_text("bad JSON", encoding="utf-8")
            call_command("loops_tick", loop=_LOOP, statusline_file=statusline, stdout=out, stderr=err)

        report = out.getvalue() + err.getvalue()
        assert "ran   loop" in report, report
        assert "WARN  open_prs: open-PR inventory unreadable" in report, report


def _next_tick(scanners: list[_InventoryScanner]) -> str:
    Loop.objects.filter(name=_LOOP).update(last_run_at=None, last_attempt_at=None)
    return _run(scanners)


def _blocked_entry(number: int, *, next_task: str = "Fix failing CI") -> PrInventoryEntry:
    return replace(_entry(number, age=timedelta(days=2), next_task=next_task), ci="red")


class TestTickRepairsStalledPrs(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.update_or_create(
            name=_LOOP,
            defaults={"enabled": True, "delay_seconds": 60, "script": f"src/teatree/loops/{_LOOP}/loop.py"},
        )

    def _stalled(self, *, state: str = Ticket.State.WORK_STARTED, planned: bool = True) -> PrInventoryEntry:
        entry = _blocked_entry(11)
        ticket = TicketFactory(state=state)
        PullRequestFactory(ticket=ticket, url=entry.url, repo=entry.repo, iid=entry.iid)
        if planned:
            PlanArtifact.objects.create(ticket=ticket, plan_text="plan", recorded_by="planner", base_sha="a" * 40)
        return entry

    def test_two_ticks_on_the_same_stall_queue_one_repair_task(self) -> None:
        entry = self._stalled()

        first = _run([_InventoryScanner("teatree", [entry])])
        second = _next_tick([_InventoryScanner("teatree", [entry])])

        (task,) = Task.objects.filter(phase="debugging")
        assert f"queued TODO-{task.pk} debugging" in first
        assert f"in flight: TODO-{task.pk} debugging" in second
        assert "Fix failing CI" in task.execution_reason

    def test_failed_repair_on_the_same_head_is_not_queued_again(self) -> None:
        entry = self._stalled()

        reports = []
        for tick in range(4):
            reports.append(
                _run([_InventoryScanner("teatree", [entry])])
                if tick == 0
                else _next_tick([_InventoryScanner("teatree", [entry])])
            )
            Task.objects.filter(phase="debugging").update(status=Task.Status.FAILED)

        assert Task.objects.filter(phase="debugging").count() == 1
        assert "queued TODO-" in reports[0]
        assert all("already attempted" in report for report in reports[1:])

    def test_task_failure_after_claim_rolls_back_the_head_ledger(self) -> None:
        entry = self._stalled(state=Ticket.State.PR_OPENED)
        row = OpenPrRow(
            ref=f"{entry.repo}!{entry.iid}",
            url=entry.url,
            age="2 d",
            next_task=entry.next_task,
            started=True,
            stall="Fix failing CI",
            head_sha=entry.head_sha,
        )

        with (
            patch.object(Ticket, "schedule_implementing", side_effect=RuntimeError("task write failed")),
            pytest.raises(RuntimeError, match="task write failed"),
        ):
            repair_stalled_prs([row])

        assert not RedMrFixAttempt.objects.filter(pr_url=entry.url).exists()

    def test_a_failing_repair_is_reported_and_leaves_no_claim_instead_of_crashing_the_tick(self) -> None:
        entry = self._stalled(state=Ticket.State.PR_OPENED)

        for index, failure in enumerate((OSError("disk"), ValueError("bad"), NoPlanArtifactError("no plan"))):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(Ticket, "schedule_implementing", side_effect=failure),
            ):
                run = _run if index == 0 else _next_tick
                report = run([_InventoryScanner("teatree", [entry])])

                assert f"WARN  pr_stall: stall repair failed ({type(failure).__name__})" in report
                assert "OPEN  souliane/teatree!11" in report
                assert not RedMrFixAttempt.objects.filter(pr_url=entry.url).exists()

    def test_a_stall_at_pr_opened_queues_debugging_once(self) -> None:
        entry = self._stalled(state=Ticket.State.PR_OPENED)

        first = _run([_InventoryScanner("teatree", [entry])])
        second = _next_tick([_InventoryScanner("teatree", [entry])])

        (task,) = Task.objects.filter(phase="debugging")
        assert f"queued TODO-{task.pk} debugging" in first
        assert f"in flight: TODO-{task.pk} debugging" in second
        assert not DeferredQuestion.objects.filter(dedupe_marker__startswith="pr-stall:").exists()

    def test_a_stall_without_a_plan_decision_routes_through_planning(self) -> None:
        entry = self._stalled(planned=False)

        report = _run([_InventoryScanner("teatree", [entry])])

        (task,) = Task.objects.all()
        assert task.phase == "planning"
        assert f"queued TODO-{task.pk} planning" in report

    def test_a_stalled_pr_no_author_ticket_owns_is_reported_but_never_acted_on(self) -> None:
        report = _run([_InventoryScanner("teatree", [_blocked_entry(12)])])

        assert "unresolved: no author ticket owns this PR" in report
        assert (Task.objects.count(), DeferredQuestion.objects.count()) == (0, 0)

    def test_a_reviewers_ticket_is_never_given_a_repair(self) -> None:
        entry = self._stalled()
        Ticket.objects.update(role=Ticket.Role.REVIEWER)

        report = _run([_InventoryScanner("teatree", [entry])])

        assert "unresolved: no author ticket owns this PR" in report
        assert Task.objects.count() == 0

    def test_a_draft_park_and_a_sweep_fault_are_not_stalls(self) -> None:
        parked = replace(_entry(13, age=timedelta(days=2), next_task="Finish draft"), reason="draft", draft=True)
        faulty = replace(
            _entry(14, age=timedelta(days=2), next_task="Inspect sweep"), reason="evaluation_failed", ci="unknown"
        )
        branch = replace(_entry(15, age=timedelta(days=2), next_task="Update branch"), decision="needs_branch_update")
        for entry in (parked, faulty, branch):
            ticket = TicketFactory(state=Ticket.State.WORK_STARTED)
            PullRequestFactory(ticket=ticket, url=entry.url, repo=entry.repo, iid=entry.iid)

        report = _run([_InventoryScanner("teatree", [parked, faulty, branch])])

        assert "STALL" not in report
        assert Task.objects.count() == 0

    def test_a_colleagues_red_pr_is_listed_but_never_a_stall(self) -> None:
        colleague = replace(_blocked_entry(16), owned=False)

        report = _run([_InventoryScanner("teatree", [colleague])])

        assert "OPEN  souliane/teatree!16" in report
        assert "STALL" not in report
        assert (Task.objects.count(), DeferredQuestion.objects.count()) == (0, 0)

    def test_a_pr_that_is_not_blocked_is_left_alone(self) -> None:
        entry = replace(self._stalled(), ci="green", next_task="Review or merge PR")

        report = _run([_InventoryScanner("teatree", [entry])])

        assert "STALL" not in report
        assert Task.objects.count() == 0

    def test_the_structured_report_names_each_repair(self) -> None:
        entry = self._stalled()

        payload = json.loads(_run([_InventoryScanner("teatree", [entry])], json_output=True))

        (task,) = Task.objects.all()
        assert payload["stall_repairs"] == [
            {"ref": "souliane/teatree!11", "cause": "Fix failing CI", "action": f"queued TODO-{task.pk} debugging"}
        ]
