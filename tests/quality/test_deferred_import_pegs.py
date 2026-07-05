"""Anti-vacuity + collision-pin for the shared per-file deferred-import peg ledger.

The real-tree gate lives in the intra-core / intra-loop ratchet test files; this
file proves the shared machinery (``_deferred_imports``) is neither vacuous nor
over-blocking, and pins the concurrent-merge property the per-file keying buys:
two disjoint peg bumps merged together keep the gate green — the scenario a
single repo-wide ``_FROZEN`` integer went red on (two independent +1s cannot
union into +2).
"""

from pathlib import Path

from tests.quality._deferred_imports import count_deferred_imports, diff_pegs, load_pegs, per_file_counts


class TestCountDeferredImports:
    def test_counts_only_function_scoped_matching_prefix(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            "from teatree.core.a import x\n"  # module scope: not counted
            "import os\n"
            "def f():\n"
            "    from teatree.core.b import y\n"  # function scope, matches: +1
            "    from teatree.other import z\n"  # function scope, wrong prefix: not counted
            "    import teatree.core.c\n",  # function-scope import, matches: +1
            encoding="utf-8",
        )
        assert count_deferred_imports(src, "teatree.core") == 2

    def test_zero_when_no_deferred_match(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text("from teatree.core.a import x\ndef f():\n    return 1\n", encoding="utf-8")
        assert count_deferred_imports(src, "teatree.core") == 0


class TestDiffPegsAntiVacuity:
    def test_over_peg_is_flagged_and_named(self) -> None:
        drift = diff_pegs({"src/a.py": 4}, {"src/a.py": 3})
        assert drift.over_peg == (("src/a.py", 4, 3),)
        assert not drift.ok
        assert "src/a.py" in "\n".join(drift.over_lines())

    def test_under_peg_demands_banking(self) -> None:
        drift = diff_pegs({"src/a.py": 2}, {"src/a.py": 3})
        assert drift.under_peg == (("src/a.py", 2, 3),)
        assert not drift.ok
        assert "src/a.py" in "\n".join(drift.under_lines())

    def test_exact_match_is_ok(self) -> None:
        assert diff_pegs({"src/a.py": 3}, {"src/a.py": 3}).ok

    def test_unlisted_file_with_a_deferral_pegs_at_zero(self) -> None:
        drift = diff_pegs({"src/new.py": 1}, {})
        assert drift.over_peg == (("src/new.py", 1, 0),)

    def test_stale_peg_with_no_live_deferral_is_under(self) -> None:
        drift = diff_pegs({}, {"src/gone.py": 2})
        assert drift.under_peg == (("src/gone.py", 0, 2),)


class TestCollisionPin:
    def test_two_disjoint_peg_bumps_merge_green(self) -> None:
        # PR-A pegs file_a to match its new deferral; PR-B pegs file_b. git unions
        # the two independent TOML keys, and the merged live tree carries both
        # deferrals -> green.
        merged_live = {"src/a.py": 1, "src/b.py": 1, "src/base.py": 5}
        merged_pegs = {"src/a.py": 1, "src/b.py": 1, "src/base.py": 5}
        assert diff_pegs(merged_live, merged_pegs).ok
        # Non-vacuity: with only ONE PR's peg edit, the merged tree is RED — the
        # other file over-pegs. So the green above is the union doing the work.
        partial_pegs = {"src/a.py": 1, "src/base.py": 5}
        assert diff_pegs(merged_live, partial_pegs).over_peg == (("src/b.py", 1, 0),)


class TestPegTablesLoad:
    def test_both_tables_present_and_nonempty(self) -> None:
        assert load_pegs("intra_core")
        assert load_pegs("intra_loop")

    def test_committed_pegs_have_no_live_drift(self) -> None:
        # Cutover smoke on the real tree: both committed peg maps equal the live
        # per-file deferred-import counts exactly (no headroom, no stale pegs).
        repo_root = Path(__file__).resolve().parents[2]
        core = repo_root / "src" / "teatree" / "core"
        loop = repo_root / "src" / "teatree" / "loop"
        assert diff_pegs(per_file_counts(core, "teatree.core"), load_pegs("intra_core")).ok
        assert diff_pegs(per_file_counts(loop, "teatree.loop"), load_pegs("intra_loop")).ok
