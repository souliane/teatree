# test-path: cross-cutting
"""Parity matrix: the four config registries derive from the model taxonomy.

The Phase-3 rewire proof. Each hand-maintained registry — ``OVERLAY_OVERRIDABLE_SETTINGS``,
``COLD_SETTINGS``, ``COLD_HOOK_SETTINGS``, ``REGISTRY_SETTINGS`` — must equal the registry
the model's ``Registry`` markers derive, key-for-key AND coercer-for-coercer. The model
(``schema.TeatreeSettingsSchema``) is the authoritative single source of the key→coercer
mapping; the runtime dicts stay hand-maintained only so the cold path never imports
pydantic. A drift in either direction turns this suite red.

Parser equality is BEHAVIORAL, not object-identity: a bound ``.parse`` classmethod and a
``_parse_overridable_positive_int(N)`` closure are fresh objects on each access, so identity
would spuriously diverge. The two keys whose resolve-tier coercer intentionally differs from
the model's storage-tier validator (``handover_mirror_path``, ``agent_harness_provider``) are
re-sourced by the derivation and so match here too.

WHY the runtime dicts stay hand-maintained instead of calling ``derive_*()``:
``teatree.config``'s package init eagerly imports all three registry-defining modules,
and the cold path (``cold_reader``) loads that package init — so a module-scope
``derive_*()`` call would drag ``schema`` -> pydantic (~110ms) onto every cold-hook
invocation. ``test_registry_modules_stay_pydantic_free`` pins that invariant.
"""

import math
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.config.schema import (
    Registry,
    _keys_in,
    derive_cold_hook_settings,
    derive_cold_settings,
    derive_overlay_overridable_settings,
    derive_registry_settings,
)
from teatree.config.setting_parsers import _parse_strict_bool, _parse_strict_int
from teatree.config.setting_registries import OVERLAY_OVERRIDABLE_SETTINGS

# A type-spanning battery: every probe exercises the accept/raise + coerced-value contract,
# so two parsers that agree on all of them are behaviorally identical for the config domain.
_PROBES: list[Any] = [
    True,
    False,
    0,
    1,
    5,
    -1,
    math.pi,
    "5",
    "0",
    "true",
    "false",
    "",
    "x",
    "claude_sdk",
    "pydantic_ai",
    "babysit",
    "off",
    "draft_or_ask",
    [],
    ["a"],
    ["a", "b"],
    {},
    {"a": 1},
    None,
]


def _outcome(fn: Any, value: Any) -> tuple[bool, Any]:
    """(raised, result) for calling *fn* on *value* — the accept/value contract."""
    try:
        return (False, fn(value))
    except Exception:  # noqa: BLE001 — parity compares raise-vs-accept, not the exception class
        return (True, None)


def _assert_parser_parity(hand: Any, derived: Any, key: str) -> None:
    for value in _PROBES:
        assert _outcome(hand, value) == _outcome(derived, value), f"{key}: coercer divergence on {value!r}"


def test_parity_helper_detects_a_genuine_divergence() -> None:
    # Control (anti-vacuity): two genuinely different coercers must be caught, so a GREEN
    # parity run below is evidence rather than a broken harness.
    with pytest.raises(AssertionError):
        _assert_parser_parity(_parse_strict_bool, _parse_strict_int, "control")


@pytest.mark.parametrize("key", sorted(OVERLAY_OVERRIDABLE_SETTINGS))
def test_overlay_registry_derives_from_model(key: str) -> None:
    derived = derive_overlay_overridable_settings()
    assert key in derived
    _assert_parser_parity(OVERLAY_OVERRIDABLE_SETTINGS[key], derived[key], key)


@pytest.mark.parametrize("key", sorted(COLD_SETTINGS))
def test_cold_registry_derives_from_model(key: str) -> None:
    derived = derive_cold_settings()
    assert key in derived
    _assert_parser_parity(COLD_SETTINGS[key], derived[key], key)


@pytest.mark.parametrize("key", sorted(REGISTRY_SETTINGS))
def test_registry_settings_derive_from_model(key: str) -> None:
    derived = derive_registry_settings()
    assert key in derived
    _assert_parser_parity(REGISTRY_SETTINGS[key], derived[key], key)


@pytest.mark.parametrize("key", sorted(COLD_HOOK_SETTINGS))
def test_cold_hook_registry_derives_from_model(key: str) -> None:
    derived = derive_cold_hook_settings()
    hand = COLD_HOOK_SETTINGS[key]
    assert key in derived
    assert derived[key].default == hand.default
    assert type(derived[key].default) is type(hand.default)
    assert derived[key].scope == hand.scope
    _assert_parser_parity(hand.parse, derived[key].parse, key)


def test_registry_modules_stay_pydantic_free() -> None:
    # The reason the four registries above stay hand-maintained rather than deriving from
    # the model at import: ``teatree.config``'s package init eagerly imports the three
    # registry-defining modules, and the cold path loads that package init, so a
    # module-scope ``derive_*()`` call would import ``schema`` -> pydantic (~110ms) onto
    # every cold-hook invocation. A fresh subprocess is the only honest probe — the test
    # process already has pydantic/schema loaded. This pins the invariant the parity
    # matrix above depends on: the ``derive_*`` helpers stay call-time only.
    probe = textwrap.dedent(
        """
        import sys
        import teatree.config.setting_registries
        import teatree.config.registries
        import teatree.config.cold_hook_settings
        assert "pydantic" not in sys.modules, "pydantic leaked onto the cold path via a registry module"
        assert "teatree.config.schema" not in sys.modules, "schema leaked onto the cold path via a registry module"
        print("clean")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


class TestDerivedKeysetsMatchTheHandRegistries:
    def test_overlay_keyset(self) -> None:
        assert set(derive_overlay_overridable_settings()) == set(OVERLAY_OVERRIDABLE_SETTINGS)

    def test_cold_keyset(self) -> None:
        assert set(derive_cold_settings()) == set(COLD_SETTINGS)

    def test_registry_keyset(self) -> None:
        assert set(derive_registry_settings()) == set(REGISTRY_SETTINGS)

    def test_cold_hook_keyset(self) -> None:
        assert set(derive_cold_hook_settings()) == set(COLD_HOOK_SETTINGS)


class TestTaxonomyPartitionsEveryKey:
    """The four ``Registry`` classes partition the whole key space with no gap or overlap."""

    def test_the_four_partitions_are_disjoint(self) -> None:
        partitions = [set(_keys_in(reg)) for reg in Registry]
        for i, left in enumerate(partitions):
            for right in partitions[i + 1 :]:
                assert left.isdisjoint(right), f"registries overlap: {left & right}"

    def test_partitions_union_is_every_known_setting(self) -> None:
        union: set[str] = set()
        for reg in Registry:
            union |= set(_keys_in(reg))
        assert union == set(ALL_KNOWN_CONFIG_SETTINGS)
