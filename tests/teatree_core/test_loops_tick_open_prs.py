"""Every tick reports every open PR it observed, with age and next task, started and oldest first."""

import io
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import django.test
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import Loop
from teatree.core.pr_inventory import PrInventoryEntry, write_inventory
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.base import ScanSignal
from teatree.loops.base import MiniLoop
from tests.factories import PullRequestFactory

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
        newest = _entry(3, age=timedelta(hours=2, minutes=5), next_task="Fix failing CI")
        oldest = _entry(1, age=timedelta(days=3, minutes=5), next_task="Resolve merge conflict")
        started = _entry(2, age=timedelta(days=1, minutes=5), next_task="Run independent review")
        PullRequestFactory(url=started.url, repo=started.repo, iid=started.iid)

        output = _run([_InventoryScanner("teatree", [newest, oldest, started])])

        lines = [line for line in output.splitlines() if "souliane/teatree!" in line]
        assert [line.split()[1] for line in lines] == ["souliane/teatree!2", "souliane/teatree!1", "souliane/teatree!3"]
        assert "1 d" in lines[0]
        assert "Run independent review" in lines[0]
        assert "3 d" in lines[1]
        assert "Resolve merge conflict" in lines[1]
        assert "2 h" in lines[2]
        assert "Fix failing CI" in lines[2]

    def test_the_structured_report_carries_the_same_rows(self) -> None:
        entry = _entry(7, age=timedelta(days=2, minutes=5), next_task="Address requested changes")

        payload = json.loads(_run([_InventoryScanner("teatree", [entry])], json_output=True))

        assert payload["open_prs"] == [
            {
                "ref": "souliane/teatree!7",
                "url": entry.url,
                "age": "2 d",
                "next_task": "Address requested changes",
                "started": False,
            }
        ]

    def test_a_pr_seen_by_two_scanners_is_listed_once(self) -> None:
        entry = _entry(9, age=timedelta(hours=5, minutes=5), next_task="Fix failing CI")

        output = _run([_InventoryScanner("own:teatree:abc", [entry]), _InventoryScanner("teatree", [entry])])

        assert output.count("souliane/teatree!9") == 1

    def test_a_tick_that_observed_nothing_lists_no_stale_snapshot_from_an_earlier_tick(self) -> None:
        with _tick([]) as statusline:
            write_inventory("teatree", [_entry(4, age=timedelta(days=1), next_task="Fix failing CI")])
            out = io.StringIO()
            call_command("loops_tick", loop=_LOOP, statusline_file=statusline, stdout=out, stderr=io.StringIO())

        assert "souliane/teatree!4" not in out.getvalue()
