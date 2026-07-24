# test-path: cross-cutting
"""The stdlib cold reader for ``defaults.toml`` — behavior, mtime cache, and coherence.

``cold_defaults`` is the cold-path twin of ``schema.shipped_defaults``: it reads the same
shipped ``defaults.toml`` with ``tomllib`` (no pydantic, no Django), so a hook leaf gets a
key's shipped default without the ~110ms model import. The load-bearing guard is the
COHERENCE test — the stdlib default equals the model default for every Default-category key —
plus the CONTROL that importing the module never pulls pydantic onto the cold path.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from teatree.config import cold_defaults
from teatree.config.cold_defaults import default_for, shipped_defaults_table
from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.schema import Category, TeatreeSettingsSchema, setting_meta, shipped_defaults

_FIELDS = TeatreeSettingsSchema.model_fields
_DEFAULT_KEYS = sorted(k for k in _FIELDS if setting_meta(k).category is Category.DEFAULT)
_NON_DEFAULT_KEYS = sorted(k for k in _FIELDS if setting_meta(k).category is not Category.DEFAULT)


class TestReadsTheShippedTable:
    def test_table_carries_the_default_category_keys(self) -> None:
        assert set(shipped_defaults_table()) == set(_DEFAULT_KEYS)

    def test_default_for_scalar_key(self) -> None:
        assert default_for("agent_harness") == "claude_sdk"

    def test_default_for_sub_table_key(self) -> None:
        assert cold_defaults.default_for("speak") == {"local": "off", "slack": False}

    def test_absent_key_returns_fallback(self) -> None:
        # A Secret/Personal key is absent from the file by construction → its empty code default.
        assert cold_defaults.default_for("slack_user_id", "") == ""
        assert cold_defaults.default_for("banned_terms", []) == []

    def test_returned_table_is_a_copy(self) -> None:
        table = cold_defaults.shipped_defaults_table()
        table["agent_harness"] = "__mutated__"
        assert cold_defaults.default_for("agent_harness") == "claude_sdk"


class TestMtimeKeyedCache:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert cold_defaults.shipped_defaults_table(tmp_path / "nope.toml") == {}
        assert cold_defaults.default_for("agent_harness", "fb", path=tmp_path / "nope.toml") == "fb"

    def test_rewrite_with_new_mtime_is_reparsed(self, tmp_path: Path) -> None:
        toml = tmp_path / "defaults.toml"
        toml.write_text('[teatree]\nagent_harness = "first"\n')

        os.utime(toml, ns=(1_000_000_000, 1_000_000_000))
        assert cold_defaults.default_for("agent_harness", path=toml) == "first"

        toml.write_text('[teatree]\nagent_harness = "second"\n')
        os.utime(toml, ns=(2_000_000_000, 2_000_000_000))
        assert cold_defaults.default_for("agent_harness", path=toml) == "second"

    def test_same_mtime_serves_the_cached_parse(self, tmp_path: Path) -> None:
        # Control: with the mtime pinned, a content change is NOT observed — proving the
        # cache is real (and that the mtime bump above is what actually invalidated it).

        toml = tmp_path / "defaults.toml"
        toml.write_text('[teatree]\nagent_harness = "first"\n')
        os.utime(toml, ns=(5_000_000_000, 5_000_000_000))
        assert cold_defaults.default_for("agent_harness", path=toml) == "first"

        toml.write_text('[teatree]\nagent_harness = "second"\n')
        os.utime(toml, ns=(5_000_000_000, 5_000_000_000))
        assert cold_defaults.default_for("agent_harness", path=toml) == "first"


class TestCoherenceWithTheModel:
    """The cold stdlib default equals the pydantic model default, per key."""

    @pytest.mark.parametrize("key", _DEFAULT_KEYS)
    def test_cold_default_equals_model_default(self, key: str) -> None:
        assert cold_defaults.default_for(key, "__MISSING__") == getattr(shipped_defaults(), key)

    @pytest.mark.parametrize("key", _NON_DEFAULT_KEYS)
    def test_non_default_key_absent_from_the_cold_table(self, key: str) -> None:
        assert cold_defaults.default_for(key, "__SENTINEL__") == "__SENTINEL__"

    @pytest.mark.parametrize("key", sorted(COLD_HOOK_SETTINGS))
    def test_cold_default_equals_cold_hook_hand_default(self, key: str) -> None:
        # The stdlib reader can serve the cold-hook default tier: its value equals the
        # hand-maintained ``ColdHookSetting.default`` for every cold-hook gate flag / budget.
        assert cold_defaults.default_for(key, "__MISSING__") == COLD_HOOK_SETTINGS[key].default


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
