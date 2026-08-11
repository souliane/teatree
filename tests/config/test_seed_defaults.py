# test-path: cross-cutting
"""The stdlib reader for the shipped ``[loops]`` / ``[modes]`` / ``[schedules]`` tables.

The seed tables are shipped defaults an operator tunes exactly like a ``[teatree]`` key,
so they live in the same packaged ``defaults.toml`` and are read the same way: stdlib
``tomllib``, mtime-cached, never pydantic and never Django. The CONTENT (which loops ship,
at which cadence) is pinned against the seeds themselves in ``tests/teatree_loops``; this
suite pins the reader and the file's table shape.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from teatree.config import seed_defaults
from teatree.config.seed_defaults import (
    SEED_ROW_FIELDS,
    SEED_TABLES,
    SHIPPED_ONLY_FIELDS,
    classify_seed_field,
    reset_seed_defaults_cache,
    seed_divergences,
    shipped_seed_table,
)


class TestReadsTheShippedSeedTables:
    def test_every_seed_table_is_present_and_non_empty(self) -> None:
        for table in SEED_TABLES:
            assert shipped_seed_table(table), f"[{table}] is missing from the shipped file"

    def test_seed_tables_are_exactly_loops_modes_schedules(self) -> None:
        assert SEED_TABLES == ("loops", "modes", "schedules")

    def test_a_loop_entry_carries_its_cadence_and_description(self) -> None:
        inbox = shipped_seed_table("loops")["inbox"]
        assert inbox["delay_seconds"] == 60
        assert inbox["default_enabled"] is True
        assert inbox["description"]

    def test_a_schedule_entry_carries_its_slots_as_an_array_of_tables(self) -> None:
        standard = shipped_seed_table("schedules")["standard"]
        assert standard["timezone"] == "Europe/Vienna"
        assert [slot["preset_name"] for slot in standard["slots"]] == ["present", "away", "away"]

    def test_an_absent_table_reads_as_empty(self) -> None:
        assert shipped_seed_table("__not_a_table__") == {}

    def test_returned_entries_are_copies(self) -> None:
        shipped_seed_table("loops")["inbox"]["delay_seconds"] = -1
        assert shipped_seed_table("loops")["inbox"]["delay_seconds"] == 60


class TestTheInterchangeSurface:
    """What ``config_setting export``/``import`` carries for a seed row, and what it does not."""

    def test_every_shipped_entry_declares_every_interchange_field(self) -> None:
        # `seed_divergences` compares a live value against `entry.get(field)`, so a field the
        # file omits would read as `None` and make an untouched row look diverged. The file
        # therefore spells out every interchange field on every entry — except `daily_at`,
        # whose absence genuinely MEANS "no daily slot" (the dataclass default is None).
        for table, fields in SEED_ROW_FIELDS.items():
            required = set(fields) - {"daily_at"}
            for name, entry in shipped_seed_table(table).items():
                assert required <= set(entry), f"[{table}.{name}] omits {sorted(required - set(entry))}"

    def test_shipped_only_fields_are_absent_from_the_interchange(self) -> None:
        for table, fields in SHIPPED_ONLY_FIELDS.items():
            assert not set(fields) & set(SEED_ROW_FIELDS[table])

    def test_an_untouched_row_set_has_no_divergence(self) -> None:
        for table in SEED_TABLES:
            live = {
                name: {field: entry.get(field) for field in SEED_ROW_FIELDS[table]}
                for name, entry in shipped_seed_table(table).items()
            }
            assert seed_divergences(table, live) == {}

    def test_a_moved_field_is_the_only_divergence_reported(self) -> None:
        assert seed_divergences("loops", {"inbox": {"delay_seconds": 90, "colleague_facing": False}}) == {
            "inbox": {"delay_seconds": 90}
        }

    def test_classification_covers_unknown_wrong_typed_and_shipped_only(self) -> None:
        assert classify_seed_field("loops", "inbox", "delay_seconds", 60) == ("skip", "")
        assert classify_seed_field("loops", "inbox", "delay_seconds", 90) == ("write", "")
        assert classify_seed_field("loops", "nope", "delay_seconds", 90)[0] == "reject"
        assert classify_seed_field("loops", "inbox", "script", "x") == ("reject", "unknown field")

    def test_a_bool_is_not_accepted_where_an_int_is_declared(self) -> None:
        # `isinstance(True, int)` is True in Python, so a naive type check would let
        # `delay_seconds = true` through and store a boolean cadence. The control is the
        # second line: the same value IS accepted where a bool is what the field declares.
        yes = True
        assert classify_seed_field("loops", "inbox", "delay_seconds", yes) == ("reject", "invalid: expected int")
        assert classify_seed_field("loops", "inbox", "colleague_facing", yes)[0] != "reject"


class TestMtimeKeyedCache:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert shipped_seed_table("loops", tmp_path / "nope.toml") == {}

    def test_rewrite_with_new_mtime_is_reparsed(self, tmp_path: Path) -> None:
        toml = tmp_path / "defaults.toml"
        toml.write_text("[loops.inbox]\ndelay_seconds = 1\n")
        os.utime(toml, ns=(1_000_000_000, 1_000_000_000))
        assert shipped_seed_table("loops", toml)["inbox"]["delay_seconds"] == 1

        toml.write_text("[loops.inbox]\ndelay_seconds = 2\n")
        os.utime(toml, ns=(2_000_000_000, 2_000_000_000))
        assert shipped_seed_table("loops", toml)["inbox"]["delay_seconds"] == 2

    def test_same_mtime_serves_the_cached_parse(self, tmp_path: Path) -> None:
        # Control: with the mtime pinned, a content change is NOT observed — proving the
        # cache is real (and that the mtime bump above is what actually invalidated it).
        toml = tmp_path / "defaults.toml"
        toml.write_text("[loops.inbox]\ndelay_seconds = 1\n")
        os.utime(toml, ns=(7_000_000_000, 7_000_000_000))
        assert shipped_seed_table("loops", toml)["inbox"]["delay_seconds"] == 1

        toml.write_text("[loops.inbox]\ndelay_seconds = 2\n")
        os.utime(toml, ns=(7_000_000_000, 7_000_000_000))
        assert shipped_seed_table("loops", toml)["inbox"]["delay_seconds"] == 1

    def test_two_files_sharing_an_mtime_do_not_serve_each_others_parse(self, tmp_path: Path) -> None:
        # An mtime-ONLY key collides across files: a coarse-granularity filesystem, or a
        # test that pins `os.utime`, makes one fixture's parse win for a file it was never
        # read from. The path is part of the identity, so it is part of the key.
        first, second = tmp_path / "a.toml", tmp_path / "b.toml"
        first.write_text("[loops.inbox]\ndelay_seconds = 1\n")
        second.write_text("[loops.inbox]\ndelay_seconds = 2\n")
        for path in (first, second):
            os.utime(path, ns=(9_000_000_000, 9_000_000_000))

        assert shipped_seed_table("loops", first)["inbox"]["delay_seconds"] == 1
        assert shipped_seed_table("loops", second)["inbox"]["delay_seconds"] == 2

    def test_the_reset_clears_the_memo_so_a_rewritten_fixture_is_re_read(self, tmp_path: Path) -> None:
        # The disposition the roster records: the conftest autouse reset must actually
        # empty the memo, so a fixture parsed in one test cannot survive into the next.
        toml = tmp_path / "defaults.toml"
        toml.write_text("[loops.inbox]\ndelay_seconds = 1\n")
        os.utime(toml, ns=(8_000_000_000, 8_000_000_000))
        assert shipped_seed_table("loops", toml)["inbox"]["delay_seconds"] == 1

        reset_seed_defaults_cache()

        toml.write_text("[loops.inbox]\ndelay_seconds = 2\n")
        os.utime(toml, ns=(8_000_000_000, 8_000_000_000))
        assert shipped_seed_table("loops", toml)["inbox"]["delay_seconds"] == 2


def test_a_non_table_entry_is_skipped_rather_than_returned(tmp_path: Path) -> None:
    toml = tmp_path / "defaults.toml"
    toml.write_text('loops = "not-a-table"\n[modes.off]\ndescription = "x"\n')
    assert shipped_seed_table("loops", toml) == {}
    assert shipped_seed_table("modes", toml) == {"off": {"description": "x"}}


def test_the_default_path_is_resolved_at_call_time_not_bound_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Same trap ``cold_defaults`` fell into: a default ARGUMENT would bind the path at
    # import, so a re-pointed module constant would be silently ignored by no-argument
    # callers and the seed set could come from a different file than the values.
    fixture = tmp_path / "defaults.toml"
    fixture.write_text("[loops.sentinel]\ndelay_seconds = 9\n", encoding="utf-8")
    monkeypatch.setattr(seed_defaults, "DEFAULTS_TOML", fixture)
    assert shipped_seed_table("loops") == {"sentinel": {"delay_seconds": 9}}


def test_import_does_not_load_pydantic_or_django() -> None:
    # The seed reader shares ``defaults.toml`` with the cold-path defaults reader, so it
    # keeps the same stdlib-only contract: a fresh subprocess is the only honest probe.
    probe = textwrap.dedent(
        """
        import sys
        import teatree.config.seed_defaults as sd
        sd.shipped_seed_table("loops")
        assert "pydantic" not in sys.modules, "pydantic leaked onto the stdlib seed reader"
        assert "django" not in sys.modules, "django leaked onto the stdlib seed reader"
        assert "teatree.config.schema" not in sys.modules, "schema leaked onto the stdlib seed reader"
        print("clean")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
