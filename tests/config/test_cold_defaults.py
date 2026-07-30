# test-path: cross-cutting
"""The stdlib reader the resolver's DEFAULTS tier parses ``defaults.toml`` through.

``cold_defaults`` reads the shipped file with ``tomllib`` (no pydantic, no Django) because
``teatree.config``'s package init imports ``resolution`` and the cold hook path loads that
init. The load-bearing guard is therefore the CONTROL that importing the module never pulls
pydantic or Django onto the cold path; the table's CONTENT (which keys ship, at which
values) is pinned against the resolver and the registries in ``test_toml_default_tier``.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from teatree.config import cold_defaults
from teatree.config.cold_defaults import flatten_settings_table, shipped_defaults_table
from teatree.config.schema import Category, TeatreeSettingsSchema, setting_meta

_FIELDS = TeatreeSettingsSchema.model_fields
_DEFAULT_KEYS = sorted(k for k in _FIELDS if setting_meta(k).category is Category.DEFAULT)
_NON_DEFAULT_KEYS = sorted(k for k in _FIELDS if setting_meta(k).category is not Category.DEFAULT)


class TestReadsTheShippedTable:
    def test_table_carries_exactly_the_default_category_keys(self) -> None:
        # Secret/Personal keys are absent by construction — they hold their empty code
        # default and are never written to a shareable file.
        assert set(shipped_defaults_table()) == set(_DEFAULT_KEYS)
        assert not set(shipped_defaults_table()) & set(_NON_DEFAULT_KEYS)

    def test_a_scalar_and_a_sub_table_key_both_parse(self) -> None:
        table = shipped_defaults_table()
        assert table["agent_harness"] == "claude_sdk"
        assert table["speak"] == {"local": "off", "slack": False}

    def test_returned_table_is_a_copy(self) -> None:
        shipped_defaults_table()["agent_harness"] = "__mutated__"
        assert shipped_defaults_table()["agent_harness"] == "claude_sdk"


class TestNestedAndFlatReadIdentically:
    """The file nests the group hierarchy; the KEY NAMESPACE the reader serves stays flat."""

    _NESTED = """\
[teatree.Workspace."Engagement & identity"]
autoload = false

[teatree.Agents."Mode & harness"]
agent_harness = "claude_sdk"

[teatree.speak]
local = "off"
slack = false
"""
    _FLAT = """\
[teatree]
agent_harness = "claude_sdk"
autoload = false

[teatree.speak]
local = "off"
slack = false
"""

    def _read(self, tmp_path: Path, text: str, stamp: int) -> dict[str, object]:
        toml = tmp_path / f"defaults-{stamp}.toml"
        toml.write_text(text, encoding="utf-8")
        return shipped_defaults_table(toml)

    def test_the_two_shapes_parse_to_one_identical_flat_mapping(self, tmp_path: Path) -> None:
        nested = self._read(tmp_path, self._NESTED, 1)
        flat = self._read(tmp_path, self._FLAT, 2)
        expected = {"agent_harness": "claude_sdk", "autoload": False, "speak": {"local": "off", "slack": False}}
        assert nested == flat == expected

    def test_a_declared_sub_table_setting_stays_a_value_never_a_group(self) -> None:
        # The disambiguation, exercised on both sides: ``speak`` is a declared setting, so
        # its table is its VALUE; ``Workspace`` declares nothing, so it is a wrapper.
        flattened = flatten_settings_table({"Workspace": {"autoload": True}, "speak": {"local": "all"}})
        assert flattened == {"autoload": True, "speak": {"local": "all"}}

    def test_an_already_flat_table_is_returned_unchanged(self) -> None:
        rows = {"autoload": True, "merge_wip": 2}
        assert flatten_settings_table(rows) == rows

    def test_a_group_wrapper_nests_to_any_depth(self) -> None:
        deep = {"Infrastructure": {"Resource pressure": {"Thresholds & cadence": {"disk_warn_free_gb": 5}}}}
        assert flatten_settings_table(deep) == {"disk_warn_free_gb": 5}


class TestMtimeKeyedCache:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert shipped_defaults_table(tmp_path / "nope.toml") == {}

    def test_rewrite_with_new_mtime_is_reparsed(self, tmp_path: Path) -> None:
        toml = tmp_path / "defaults.toml"
        toml.write_text('[teatree]\nagent_harness = "first"\n')

        os.utime(toml, ns=(1_000_000_000, 1_000_000_000))
        assert shipped_defaults_table(toml)["agent_harness"] == "first"

        toml.write_text('[teatree]\nagent_harness = "second"\n')
        os.utime(toml, ns=(2_000_000_000, 2_000_000_000))
        assert shipped_defaults_table(toml)["agent_harness"] == "second"

    def test_same_mtime_serves_the_cached_parse(self, tmp_path: Path) -> None:
        # Control: with the mtime pinned, a content change is NOT observed — proving the
        # cache is real (and that the mtime bump above is what actually invalidated it).

        toml = tmp_path / "defaults.toml"
        toml.write_text('[teatree]\nagent_harness = "first"\n')
        os.utime(toml, ns=(5_000_000_000, 5_000_000_000))
        assert shipped_defaults_table(toml)["agent_harness"] == "first"

        toml.write_text('[teatree]\nagent_harness = "second"\n')
        os.utime(toml, ns=(5_000_000_000, 5_000_000_000))
        assert shipped_defaults_table(toml)["agent_harness"] == "first"


def test_the_module_exposes_only_what_the_resolver_consumes() -> None:
    # A reader nothing calls is the inverse-drift class this package now ratchets: the
    # module's public surface is exactly the path constant + the table the DEFAULTS tier
    # (``resolution._toml_default_rows``) and ``schema`` resolve the file through.
    assert set(cold_defaults.__all__) == {"DEFAULTS_TOML", "flatten_settings_table", "shipped_defaults_table"}
    public = {name for name in vars(cold_defaults) if not name.startswith("_")}
    assert public - set(cold_defaults.__all__) <= {"Any", "Mapping", "Path", "threading", "tomllib"}


def test_import_does_not_load_pydantic_or_django() -> None:
    # The cold-path invariant: importing this module keeps the ~110ms pydantic import (and
    # Django) off the cold path. A fresh subprocess is the only honest probe — the test
    # process already has both loaded.
    probe = textwrap.dedent(
        """
        import sys
        import teatree.config.cold_defaults as cd
        cd.shipped_defaults_table()
        assert "pydantic" not in sys.modules, "pydantic leaked onto the cold path"
        assert "django" not in sys.modules, "django leaked onto the cold path"
        assert "teatree.config.schema" not in sys.modules, "schema leaked onto the cold path"
        print("clean")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


def test_the_default_path_is_resolved_at_call_time_not_bound_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Binding DEFAULTS_TOML as a default ARGUMENT made a re-pointed module constant
    # silently invisible to every no-argument caller, while `resolution._toml_default_rows`
    # (which passes it explicitly) honoured it — so the shipped key SET and the shipped
    # VALUES could be read from two different files at once.
    fixture = tmp_path / "defaults.toml"
    fixture.write_text('[teatree]\nmode = "sentinel"\n', encoding="utf-8")
    monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", fixture)
    assert cold_defaults.shipped_defaults_table() == {"mode": "sentinel"}
