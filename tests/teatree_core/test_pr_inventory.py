import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import pytest

from teatree.core.pr_inventory import PrInventoryEntry, read_inventory, write_inventory


def _entry(number: int) -> PrInventoryEntry:
    return PrInventoryEntry(
        url=f"https://github.com/souliane/teatree/pull/{number}",
        repo="souliane/teatree",
        iid=number,
        title=f"PR {number}",
        created_at="2026-10-01T10:00:00Z",
        updated_at="2026-10-02T10:00:00Z",
        next_task="review",
        decision="awaiting_review",
        ci="green",
        approvals="unknown",
        threads="unknown",
        draft=False,
    )


class PrInventoryTestCase(TestCase):
    def test_inventory_preserves_twenty_five_prs_and_overlay_identity(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("teatree", [_entry(i) for i in range(1, 26)], directory=directory, observed_at=at)
            write_inventory("other", [_entry(30)], directory=directory, observed_at=at)

            snapshots = read_inventory(directory=directory)
            assert {snapshot.overlay: len(snapshot.entries) for snapshot in snapshots} == {"teatree": 25, "other": 1}
            assert snapshots[0].observed_at == at
            assert snapshots[0].entries[0].next_task == "review"

    def test_empty_successful_inventory_replaces_prior_open_set(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("teatree", [_entry(1)], directory=directory)
            write_inventory("teatree", [], directory=directory)

            snapshots = read_inventory(directory=directory)
            assert len(snapshots) == 1
            assert snapshots[0].entries == ()

    def test_unreadable_snapshot_fails_loudly(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("teatree", [_entry(1)], directory=directory)
            next(directory.glob("*.json")).write_text("bad JSON", encoding="utf-8")

            with pytest.raises(ValueError, match="inventory"):
                read_inventory(directory=directory)

    def test_malformed_snapshot_types_fail_loudly(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            target = directory / "malformed.json"
            for payload in (
                {"overlay": 42, "observed_at": "2026-10-23T12:00:00+00:00", "entries": []},
                {"overlay": "teatree", "observed_at": "2026-10-23T12:00:00+00:00", "entries": ["bad"]},
                {
                    "overlay": "teatree",
                    "observed_at": "2026-10-23T12:00:00+00:00",
                    "entries": [{**asdict(_entry(1)), "url": 7}],
                },
            ):
                with self.subTest(payload=payload):
                    target.write_text(json.dumps(payload), encoding="utf-8")
                    with pytest.raises(ValueError, match="inventory"):
                        read_inventory(directory=directory)
