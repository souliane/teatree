"""Tests for the pure visual-baseline detection engine.

The engine decides, from a staged path list alone, which files are Playwright
visual-regression baselines (``__snapshots__/`` or ``<spec>-snapshots/``). The
DB attestation lookup lives in the hook; this covers only the path matcher and
the refusal message.
"""

from teatree.quality.snapshot_baseline import block_message, is_snapshot_baseline, snapshot_baselines


class TestIsSnapshotBaseline:
    def test_snapshots_dir_matches(self) -> None:
        assert is_snapshot_baseline("e2e/__snapshots__/login-chromium.png")
        assert is_snapshot_baseline("__snapshots__/home.png")

    def test_per_spec_snapshots_dir_matches(self) -> None:
        assert is_snapshot_baseline("e2e/login.spec.ts-snapshots/hero-chromium.png")
        assert is_snapshot_baseline("packages/w/e2e/cart.spec.ts-snapshots/a-darwin.png")

    def test_nested_baseline_matches(self) -> None:
        assert is_snapshot_baseline("a/b/c/__snapshots__/deep/thing.txt")

    def test_source_file_mentioning_snapshot_does_not_match(self) -> None:
        # A whole path *segment*, not a substring — these are not baselines.
        assert not is_snapshot_baseline("src/teatree/loop/scanners/snapshot_warmer.py")
        assert not is_snapshot_baseline("tests/test_snapshot.py")
        assert not is_snapshot_baseline("my-snapshots-util.ts")

    def test_regular_spec_is_not_a_baseline(self) -> None:
        assert not is_snapshot_baseline("e2e/login.spec.ts")


class TestSnapshotBaselines:
    def test_filters_and_preserves_order(self) -> None:
        paths = [
            "README.md",
            "e2e/__snapshots__/b.png",
            "src/app.py",
            "e2e/a.spec.ts-snapshots/x.png",
        ]
        assert snapshot_baselines(paths) == [
            "e2e/__snapshots__/b.png",
            "e2e/a.spec.ts-snapshots/x.png",
        ]

    def test_empty_when_no_baselines(self) -> None:
        assert snapshot_baselines(["README.md", "src/app.py"]) == []


class TestBlockMessage:
    def test_message_names_files_ticket_and_record_command(self) -> None:
        msg = block_message(
            ["e2e/__snapshots__/a.png"],
            ticket_ref="42",
            record_command="t3 <overlay> lifecycle record-e2e-run 42 ...",
        )
        assert "e2e/__snapshots__/a.png" in msg
        assert "42" in msg
        assert "record-e2e-run 42" in msg
        assert "ALLOW_SNAPSHOT_BASELINE" in msg
