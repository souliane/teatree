# test-path: cross-cutting
"""``config/resolution.py`` docs tell the DB-only truth, not a live TOML tier (CFG-7).

The file config tier was removed — every ``UserSettings`` field is DB-home and the
``SettingHome.TOML`` carve-out is retained but EMPTY. The module's docstrings once
described a LIVE ``DB/TOML`` two-home partition with per-overlay TOML resolution
chains; this drift guard goes RED if that stale live-tier vocabulary regresses.
Anti-vacuous: it fails on the exact phrases the correction removed and requires the
module docstring to state the DB-only reality.
"""

import inspect

import teatree.config.resolution as resolution_mod

# Phrases that describe TOML as a LIVE resolution tier — the stale framing removed.
# Mentions of the retained EMPTY carve-out (``SettingHome.TOML`` / ``_toml_home`` /
# "TOML-home carve-out is ... EMPTY") are legitimate and deliberately not banned.
_BANNED_LIVE_TIER_PHRASES = (
    "DB/TOML hard partition",
    "per-overlay TOML layer",
    "per-overlay TOML -",
    "TOML two-tier",
    "TOML tables are not\nread",
)


def test_resolution_source_has_no_live_toml_tier_phrasing() -> None:
    source = inspect.getsource(resolution_mod)
    for phrase in _BANNED_LIVE_TIER_PHRASES:
        assert phrase not in source, f"stale live-TOML-tier phrasing {phrase!r} in config/resolution.py"


def test_module_docstring_states_the_db_only_reality() -> None:
    doc = resolution_mod.__doc__ or ""
    assert "DB-home partition" in doc
    assert "file config tier was removed" in doc
