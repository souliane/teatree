# test-path: cross-cutting
"""Cold-safe flattening of the generated settings table."""

import subprocess
import sys
import textwrap
import tomllib

from teatree.config import cold_defaults
from teatree.config.cold_defaults import DEFAULTS_TOML, flatten_settings_table
from teatree.config.schema import Category, TeatreeSettingsSchema, setting_meta

_FIELDS = TeatreeSettingsSchema.model_fields
_DEFAULT_KEYS = sorted(k for k in _FIELDS if setting_meta(k).category is Category.DEFAULT)
_NON_DEFAULT_KEYS = sorted(k for k in _FIELDS if setting_meta(k).category is not Category.DEFAULT)


def _shipped_table() -> dict[str, object]:
    return flatten_settings_table(tomllib.loads(DEFAULTS_TOML.read_text(encoding="utf-8"))["teatree"])


def test_generated_table_carries_exactly_default_category_keys() -> None:
    table = _shipped_table()
    assert set(table) == set(_DEFAULT_KEYS)
    assert not set(table) & set(_NON_DEFAULT_KEYS)
    assert table["agent_harness"] == "claude_sdk"
    assert table["speak"] == {"local": "off", "slack": False}


def test_nested_and_flat_tables_have_identical_keys_and_values() -> None:
    nested = tomllib.loads("""[teatree.Workspace."Engagement & identity"]
autoload = false
[teatree.Agents."Mode & harness"]
agent_harness = "claude_sdk"
[teatree.speak]
local = "off"
slack = false
""")["teatree"]
    flat = tomllib.loads("""[teatree]
agent_harness = "claude_sdk"
autoload = false
[teatree.speak]
local = "off"
slack = false
""")["teatree"]
    expected = {"agent_harness": "claude_sdk", "autoload": False, "speak": {"local": "off", "slack": False}}
    assert flatten_settings_table(nested) == flatten_settings_table(flat) == expected


def test_declared_subtable_remains_a_value() -> None:
    table = {"Workspace": {"autoload": True}, "speak": {"local": "all"}}
    assert flatten_settings_table(table) == {"autoload": True, "speak": {"local": "all"}}


def test_flat_table_is_unchanged() -> None:
    rows = {"autoload": True, "merge_wip": 2}
    assert flatten_settings_table(rows) == rows


def test_group_wrapper_nests_to_any_depth() -> None:
    deep = {"Infrastructure": {"Resource pressure": {"Thresholds & cadence": {"disk_warn_free_gb": 5}}}}
    assert flatten_settings_table(deep) == {"disk_warn_free_gb": 5}


def test_module_exposes_only_its_reader_contract() -> None:
    assert set(cold_defaults.__all__) == {"DEFAULTS_TOML", "flatten_settings_table"}


def test_import_does_not_load_pydantic_or_django() -> None:
    probe = textwrap.dedent(
        """
        import sys
        import teatree.config.cold_defaults as cd
        cd.flatten_settings_table({"Workspace": {"autoload": True}})
        assert "pydantic" not in sys.modules, "pydantic leaked onto the cold path"
        assert "django" not in sys.modules, "django leaked onto the cold path"
        assert "teatree.config.schema" not in sys.modules, "schema leaked onto the cold path"
        print("clean")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
