import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import pytest

from teatree.core.review.pr_inventory import (
    PrInventoryEntry,
    PrInventoryError,
    PrInventorySnapshot,
    duration_parts,
    elapsed_label,
    mark_inventory_unknown,
    merged_entries,
    read_inventory,
    write_inventory,
)
from teatree.core.review.pr_next_action import stall_cause


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
    def test_duration_parts_preserve_day_hour_and_minute_precision(self) -> None:
        now = datetime(2026, 10, 23, 12, tzinfo=UTC)
        assert duration_parts(now - timedelta(days=2, hours=3, minutes=5), now) == (2, 3, 5)
        assert duration_parts(now + timedelta(minutes=1), now) == (0, 0, 0)
        assert duration_parts(None, now) is None

    def test_colleague_pr_is_not_reported_as_a_factory_stall(self) -> None:
        assert stall_cause(replace(_entry(1), owned=False, ci="red", next_task="Fix failing CI")) == ""

    def test_sweep_keeps_a_more_precise_approval_count_than_the_own_scan(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        own = replace(_entry(1), head_sha="a" * 40, approvals="approved", approvals_display="approved")
        sweep = replace(_entry(1), head_sha="a" * 40, approvals="approved", approvals_display="1/unknown (approved)")
        snapshots = (
            PrInventorySnapshot("own:teatree:github", at, (own,)),
            PrInventorySnapshot("teatree", at, (sweep,)),
        )
        assert merged_entries(snapshots)[own.url].approvals_display == "1/unknown (approved)"

    def test_review_facts_from_a_prior_head_do_not_cross_to_a_new_head(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        own = replace(_entry(1), head_sha="a" * 40, approvals="approved", threads="0 unresolved")
        sweep = replace(_entry(1), head_sha="b" * 40, approvals="unknown", threads="unknown")
        snapshots = (
            PrInventorySnapshot("own:teatree:github", at, (own,)),
            PrInventorySnapshot("teatree", at, (sweep,)),
        )
        entry = merged_entries(snapshots)[own.url]
        assert (entry.approvals, entry.threads) == ("unknown", "unknown")

    def test_newer_sweep_review_verdict_wins_over_owned_snapshot_on_the_same_head(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        own = replace(
            _entry(1),
            head_sha="a" * 40,
            approvals="approved",
            approvals_display="1/1 (approved)",
            threads="0 unresolved",
        )
        sweep = replace(
            _entry(1),
            head_sha="a" * 40,
            approvals="awaiting approval",
            approvals_display="0/1 (awaiting approval)",
            threads="1 unresolved",
        )
        snapshots = (
            PrInventorySnapshot("own:teatree:github", at, (own,)),
            PrInventorySnapshot("teatree", at + timedelta(minutes=1), (sweep,)),
        )
        entry = merged_entries(snapshots)[own.url]
        assert (entry.approvals, entry.approvals_display, entry.threads) == (
            "awaiting approval",
            "0/1 (awaiting approval)",
            "1 unresolved",
        )

    def test_newer_own_ci_failure_wins_over_older_green_sweep(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        sweep = replace(_entry(1), head_sha="a" * 40, ci="green")
        own = replace(_entry(1), head_sha="a" * 40, ci="red")
        snapshots = (
            PrInventorySnapshot("own:teatree:github", at + timedelta(minutes=1), (own,)),
            PrInventorySnapshot("teatree", at, (sweep,)),
        )
        assert merged_entries(snapshots)[own.url].ci == "red"

    def test_newer_unknown_thread_read_is_not_filled_from_an_older_zero(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        own = replace(_entry(1), head_sha="a" * 40, threads="0 unresolved")
        sweep = replace(_entry(1), head_sha="a" * 40, threads="unknown")
        snapshots = (
            PrInventorySnapshot("own:teatree:github", at, (own,)),
            PrInventorySnapshot("teatree", at + timedelta(minutes=1), (sweep,)),
        )
        assert merged_entries(snapshots)[own.url].threads == "unknown"

    def test_age_under_one_minute_is_labelled_without_rounding_up(self) -> None:
        now = datetime(2026, 10, 24, 12, tzinfo=UTC)
        assert elapsed_label(now - timedelta(seconds=30), now) == "< 1 min"

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

    def test_marking_unknown_keeps_the_prior_observation_but_no_longer_verifies_it(self) -> None:
        at = datetime(2026, 10, 23, 12, tzinfo=UTC)
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory(
                "teatree", [_entry(1)], directory=directory, observed_at=at, watched_repos=("souliane/teatree",)
            )
            mark_inventory_unknown("teatree", directory=directory)

            (snapshot,) = read_inventory(directory=directory)
            assert (snapshot.verified, snapshot.observed_at, snapshot.watched_repos) == (
                False,
                at,
                ("souliane/teatree",),
            )
            assert [entry.iid for entry in snapshot.entries] == [1]

    def test_marking_unknown_without_a_prior_snapshot_records_an_empty_unverified_one(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            mark_inventory_unknown("teatree", directory=directory)

            (snapshot,) = read_inventory(directory=directory)
            assert (snapshot.verified, snapshot.entries, snapshot.watched_repos) == (False, (), ())

    def test_a_fresh_write_verifies_an_unknown_snapshot_again(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            mark_inventory_unknown("teatree", directory=directory)
            write_inventory("teatree", [_entry(1)], directory=directory)

            (snapshot,) = read_inventory(directory=directory)
            assert snapshot.verified

    def test_malformed_snapshot_metadata_fails_loudly(self) -> None:
        base = {"overlay": "teatree", "observed_at": "2026-10-23T12:00:00+00:00", "entries": []}
        with TemporaryDirectory() as root:
            directory = Path(root)
            target = directory / "malformed.json"
            for extra in ({"watched_repos": "souliane/teatree"}, {"watched_repos": [7]}, {"verified": "yes"}):
                with self.subTest(extra=extra):
                    target.write_text(json.dumps({**base, **extra}), encoding="utf-8")
                    with pytest.raises(ValueError, match="inventory"):
                        read_inventory(directory=directory)

    def test_unreadable_snapshot_fails_loudly(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("teatree", [_entry(1)], directory=directory)
            next(directory.glob("*.json")).write_text("bad JSON", encoding="utf-8")

            with pytest.raises(ValueError, match="inventory"):
                read_inventory(directory=directory)

    def test_unreadable_source_does_not_hide_a_readable_source(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("teatree", [_entry(1)], directory=directory)
            (directory / "bad.json").write_text("bad JSON", encoding="utf-8")
            errors: list[str] = []

            snapshots = read_inventory(directory=directory, errors=errors)

        assert [entry.iid for snapshot in snapshots for entry in snapshot.entries] == [1]
        assert len(errors) == 1
        assert "Unreadable PR inventory" in errors[0]

    def test_removed_overlay_sources_are_pruned_without_hiding_registered_sources(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("registered", [_entry(1)], directory=directory)
            write_inventory("removed", [_entry(2)], directory=directory)
            write_inventory("own:removed:old-key", [_entry(3)], directory=directory)

            snapshots = read_inventory(directory=directory, active_overlays=("registered",))

            assert [snapshot.overlay for snapshot in snapshots] == ["registered"]
            assert len(list(directory.glob("*.json"))) == 1

    def test_corrupt_prior_source_can_be_invalidated_and_recovered(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            path = write_inventory("teatree", [_entry(1)], directory=directory)
            path.write_text("bad JSON", encoding="utf-8")

            mark_inventory_unknown("teatree", directory=directory)
            (unknown,) = read_inventory(directory=directory)
            write_inventory("teatree", [_entry(2)], directory=directory)
            (recovered,) = read_inventory(directory=directory)

        assert not unknown.verified
        assert recovered.verified
        assert [entry.iid for entry in recovered.entries] == [2]

    def test_new_own_source_replaces_the_old_key_for_the_same_overlay(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("own:teatree:old", [_entry(1)], directory=directory)
            write_inventory("own:teatree:new", [_entry(2)], directory=directory)
            snapshots = read_inventory(directory=directory)

        assert [(snapshot.overlay, snapshot.entries[0].iid) for snapshot in snapshots] == [("own:teatree:new", 2)]

    def test_source_key_change_prunes_only_its_host_and_preserves_other_hosts(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("own:teatree:host-a:old-scope", [_entry(1)], directory=directory)
            write_inventory("own:teatree:host-b:scope", [_entry(2)], directory=directory)
            write_inventory("own:teatree:host-a:new-scope", [_entry(3)], directory=directory)

            snapshots = read_inventory(directory=directory, active_overlays=("teatree",))

        assert {snapshot.overlay: snapshot.entries[0].iid for snapshot in snapshots} == {
            "own:teatree:host-a:new-scope": 3,
            "own:teatree:host-b:scope": 2,
        }

    def test_new_host_key_prunes_legacy_snapshot_for_the_same_host(self) -> None:
        host_hash = sha256(b"https://github.com").hexdigest()[:16]
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("own:teatree:old-scope", [_entry(1)], directory=directory)
            write_inventory(f"own:teatree:{host_hash}:new-scope", [_entry(2)], directory=directory)

            snapshots = read_inventory(directory=directory, active_overlays=("teatree",))

        assert [(snapshot.overlay, snapshot.entries[0].iid) for snapshot in snapshots] == [
            (f"own:teatree:{host_hash}:new-scope", 2)
        ]

    def test_invalid_write_is_refused_and_leaves_the_prior_snapshots_readable(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            write_inventory("teatree", [_entry(1)], directory=directory)
            wrong_type = replace(_entry(2), iid="2")

            for overlay, entries in (("", [_entry(3)]), ("teatree", [wrong_type])):
                with self.subTest(overlay=overlay), pytest.raises(PrInventoryError):
                    write_inventory(overlay, entries, directory=directory)

            snapshots = read_inventory(directory=directory)
            assert [(row.overlay, [entry.iid for entry in row.entries]) for row in snapshots] == [("teatree", [1])]
            assert list(directory.glob(".pr-inventory-*")) == []

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
