# test-path: cross-cutting
"""Conformance suite pinning ``TeatreeSettingsSchema`` + ``defaults.toml`` to the registries.

The load-bearing guard on the Phase-1 foundation: the schema must cover EXACTLY the
237 known config keys, ``defaults.toml`` must carry EXACTLY the Default-category keys
at canonical values, the taxonomy must classify every credential/secret key correctly,
and — the key risk — the model-derived per-field validator must behave IDENTICALLY to
the pre-change registry coercer (the parity matrix). A drift in any of these turns the
suite red before the 237-key mapping can silently rot.
"""

import tomllib
from typing import Any, ClassVar

import pytest
from pydantic import TypeAdapter

from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.feature_flags import dark_flags
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.schema import (
    _DEFAULTS_TOML,
    Category,
    TeatreeSettingsSchema,
    _parse_strict_str,
    _provider_or_none,
    setting_meta,
    shipped_defaults,
)
from teatree.config.secret_settings import is_credential_reference
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS

# The schema deliberately re-sources two keys at the STORAGE tier rather than reusing the
# registry's RESOLVE-tier coercer (documented in schema.py): ``handover_mirror_path`` is
# persisted as a string (the registry coercer inflates it to a Path — a non-idempotent
# str->Path map unsuitable as a stored-layer validator), and ``agent_harness_provider``'s
# None default needs a None-tolerant wrapper around the parse. Parity is asserted against
# what the schema ACTUALLY validates with, so those two use their storage coercer here.
_STORAGE_COERCER = {
    **ALL_KNOWN_CONFIG_SETTINGS,
    "handover_mirror_path": _parse_strict_str,
    "agent_harness_provider": _provider_or_none,
}

_KEYS = sorted(ALL_KNOWN_CONFIG_SETTINGS)
_SECRET_KEYS = sorted(k for k in _KEYS if setting_meta(k).category is Category.SECRET)


def _toml_teatree() -> dict[str, Any]:
    """The shipped ``[teatree]`` table in the FLAT key namespace the schema fields use.

    The file nests the keys into group sub-tables; the flat namespace is the contract, so
    every conformance assertion below is made against the flattened parse.
    """
    return flatten_settings_table(tomllib.loads(_DEFAULTS_TOML.read_text())["teatree"])


def _settings_table_text() -> str:
    """The raw text of the ``[teatree.*]`` sections, up to the first sibling top-level table.

    The block opens at the first group sub-table: the hierarchy is rendered as real nested
    tables, so ``[teatree]`` itself holds no direct keys and prints no header of its own.
    """
    raw = _DEFAULTS_TOML.read_text()
    section = raw[raw.index("\n[teatree.") :]
    return section[: section.index("\n[loops.")]


def _default_keys() -> set[str]:
    return {k for k in ALL_KNOWN_CONFIG_SETTINGS if setting_meta(k).category is Category.DEFAULT}


def _field_adapter(key: str) -> TypeAdapter[Any]:
    # rebuild_annotation() reconstructs Annotated[type, BeforeValidator(...), SettingMeta(...)],
    # so the adapter runs the EXACT per-field validation the model uses.
    return TypeAdapter(TeatreeSettingsSchema.model_fields[key].rebuild_annotation())


class TestSchemaCoversKnownSettings:
    def test_fields_match_known_settings_exactly(self) -> None:
        assert set(TeatreeSettingsSchema.model_fields) == set(ALL_KNOWN_CONFIG_SETTINGS)

    def test_every_field_carries_a_setting_meta(self) -> None:
        for key in _KEYS:
            meta = setting_meta(key)
            assert isinstance(meta.category, Category)


class TestDefaultsFileShape:
    def test_defaults_file_keys_are_exactly_the_default_category(self) -> None:
        assert set(_toml_teatree()) == _default_keys()

    def test_the_file_carries_the_settings_table_and_the_three_seed_tables(self) -> None:
        # The shipped file is one file, several top-level tables: the `[teatree]` settings
        # the resolver's DEFAULTS tier reads, plus a seed table per object family. A table
        # nobody declared here would be read by nothing.
        assert set(tomllib.loads(_DEFAULTS_TOML.read_text())) == {"teatree", "loops", "modes", "schedules"}

    def test_no_secret_or_personal_key_name_appears_in_the_settings_table(self) -> None:
        raw = _settings_table_text()
        for key in _KEYS:
            if setting_meta(key).category is Category.DEFAULT:
                continue
            # A byte scan over the `[teatree]` section: a secret/personal key must never
            # surface as a `key =` / `[..key]` assignment line. Scoped to that section
            # because the sibling seed tables are a different namespace — `[loops.<name>]`
            # names LOOPS, and one of them may legitimately collide with a setting key.
            assert f"\n{key} =" not in raw
            assert f"{key}]" not in raw

    def test_shipped_defaults_constructs(self) -> None:
        settings = shipped_defaults()
        assert isinstance(settings, TeatreeSettingsSchema)
        # Cached singleton: a second call returns the same object.
        assert shipped_defaults() is settings


class TestDefaultsAreCanonical:
    """Every default is a fixed point of its own storage coercer (re-import is a no-op)."""

    def _stored_default(self, key: str) -> Any:
        if setting_meta(key).category is Category.DEFAULT:
            return _toml_teatree()[key]
        return TeatreeSettingsSchema.model_fields[key].get_default()

    @pytest.mark.parametrize("key", _KEYS)
    def test_coercer_of_default_is_the_default(self, key: str) -> None:
        default = self._stored_default(key)
        assert _STORAGE_COERCER[key](default) == default


class TestTaxonomy:
    @pytest.mark.parametrize("key", _KEYS)
    def test_credential_reference_keys_are_secret(self, key: str) -> None:
        if is_credential_reference(key):
            assert setting_meta(key).category is Category.SECRET

    @pytest.mark.parametrize("key", _SECRET_KEYS)
    def test_secret_field_defaults_are_empty_or_none(self, key: str) -> None:
        default = TeatreeSettingsSchema.model_fields[key].get_default()
        assert default in ([], {}, "", None)


class TestSafetyAndDarkFlagsPinned:
    """Every safety-posture key and dark flag ships at exactly the literal pinned here.

    The pin is per-key and exhaustive: :meth:`test_pinned_set_covers_safety_posture_and_dark_flags`
    refuses a member of either registry that no literal below covers, so a key can only
    move by editing this table — which is the reviewed decision. ``autonomy`` is the one
    member NOT at its fail-closed literal: #3895 raised it to ``full`` on the owner's
    explicit authorization, recorded in :data:`_OWNER_RAISED` so the divergence from
    fail-closed is named rather than silent. Every other key is fail-closed/off.
    """

    #: What "safe" MEANS for each safety-posture key, independent of what ships. The
    #: audited delta against :attr:`_PINNED` is the whole point: a key whose shipped
    #: literal leaves this value needs an :attr:`_OWNER_RAISED` entry.
    _FAIL_CLOSED: ClassVar[dict[str, Any]] = {
        "autonomy": "babysit",
        "enforce_regulated_path": False,
        "regulated_path_model_allowlist": [],
        "substrate_self_signoff": False,
        "substrate_auto_merge_authorized_by": "",
        "on_behalf_post_mode": "draft_or_ask",
        "on_behalf_auto_actions": ["post_e2e_evidence"],
        "send_proxy_allowlist": [],
        "trusted_issue_authors": [],
        "bulk_close_threshold": 5,
    }

    #: Safety-posture keys deliberately shipped ABOVE their fail-closed value, each with
    #: the owner authorization that moved it. A key here is still pinned — it just has a
    #: recorded reason for its literal.
    _OWNER_RAISED: ClassVar[dict[str, str]] = {
        "autonomy": "#3895 — owner-authorised autonomous-by-default posture",
    }

    _PINNED: ClassVar[dict[str, Any]] = {
        # SAFETY_POSTURE_KEYS — the ten write-is-an-authorization keys.
        "autonomy": "full",
        "enforce_regulated_path": False,
        "regulated_path_model_allowlist": [],
        "substrate_self_signoff": False,
        "substrate_auto_merge_authorized_by": "",
        "on_behalf_post_mode": "draft_or_ask",
        "on_behalf_auto_actions": ["post_e2e_evidence"],
        "send_proxy_allowlist": [],
        "trusted_issue_authors": [],
        "bulk_close_threshold": 5,
        # DARK feature-flags — each pinned to its off value.
        "outer_loop_enabled": False,
        "factory_score_enabled": False,
        "require_plan_adequacy": False,
        "critic_gate_mode": "off",
        "send_proxy_mode": "warn",
        "require_debt_delta": False,
        "require_executed_repro": False,
        "require_merge_quality_verdict": False,
        "require_spec_coverage": False,
        "ci_eval_heal_autofix_enabled": False,
    }

    def test_pinned_set_covers_safety_posture_and_dark_flags(self) -> None:
        assert set(self._PINNED) == SAFETY_POSTURE_KEYS | set(dark_flags())

    @pytest.mark.parametrize("key", sorted(_PINNED))
    def test_shipped_default_matches_pinned_literal(self, key: str) -> None:
        assert _toml_teatree()[key] == self._PINNED[key]

    def test_every_dark_flag_is_pinned_to_its_own_off_value(self) -> None:
        # A dark flag's literal is not free-form: it must equal the flag's declared
        # off_value, so editing this table can never quietly ship a dark feature ON.
        for key, flag in dark_flags().items():
            assert self._PINNED[key] == flag.off_value

    def test_fail_closed_table_covers_every_safety_posture_key(self) -> None:
        assert set(self._FAIL_CLOSED) == SAFETY_POSTURE_KEYS

    def test_only_recorded_owner_authorizations_leave_the_fail_closed_value(self) -> None:
        # The guard on the table itself: raising a safety-posture key above its
        # fail-closed literal requires an entry naming the authorization. Without one
        # the raise is unrecorded and this goes red.
        raised = {key for key in SAFETY_POSTURE_KEYS if self._PINNED[key] != self._FAIL_CLOSED[key]}
        assert raised == set(self._OWNER_RAISED)
        assert all(self._OWNER_RAISED[key].strip() for key in raised)


# ---- The parity matrix (the load-bearing test) -----------------------------------------
# Fixtures keyed by coercer KIND: for every key, the model-derived validator must produce
# the SAME accept/raise decision and the SAME coerced value as the storage coercer.

# Each entry maps a coercer kind to a pair of value lists: the ones its coercer accepts
# and the ones it rejects.
_FIXTURES: dict[str, tuple[list[Any], list[Any]]] = {
    "bool": ([True, False], ["true", 1, [], "false"]),
    "int": ([0, 5, "5", -3], [True, 1.5, "x", []]),
    "float": ([0.0, 1.5, 2, "2.5"], [True, "x", []]),
    "str": (["", "x", "hello"], [True, 5, 1.5, []]),
    "str_list": ([[], ["a"], ["a", "b"]], ["a", True, 5, {}]),
    "aliases": ([[], ["a"], ["a", "a"]], ["a", 5]),
    "registry_dict": ([{}, {"a": 1}], ["x", [], True, 5]),
    "pos_int": ([3, "3", 0, -1], []),  # fail-SAFE: never raises, degrades to its default
    "harness": (["claude_sdk", "pydantic_ai", "custom_name"], [5]),
    "speak": ([{}, {"local": "off"}, {"local": "dm", "slack": True}], ["x", [], 5]),
    "mr_reminder": ([{}, {"channels": {}}, {"default_channel": "c"}], ["x", [], 5]),
    "provider": ([None, "api_key", "subscription_oauth"], ["__bogus__"]),
}

_KIND_BY_QUALNAME = {
    "_parse_strict_bool": "bool",
    "_parse_strict_int": "int",
    "_parse_strict_float": "float",
    "_parse_strict_str": "str",
    "_parse_str_list": "str_list",
    "_parse_user_identity_aliases": "aliases",
    "_parse_registry_dict": "registry_dict",
    "parse_harness_name": "harness",
    "parse_speak_setting": "speak",
    "parse_mr_reminder_setting": "mr_reminder",
}


def _fixture_and_valid(key: str) -> tuple[str, list[Any], list[Any]]:
    """The (kind, accepted, rejected) fixture triple for *key*'s storage coercer."""
    if key == "handover_mirror_path":
        return ("str", *_FIXTURES["str"])
    if key == "agent_harness_provider":
        return ("provider", *_FIXTURES["provider"])
    coercer = ALL_KNOWN_CONFIG_SETTINGS[key]
    qual = getattr(coercer, "__qualname__", "")
    if qual.endswith("_parse_overridable_positive_int.<locals>.parse"):
        return ("pos_int", *_FIXTURES["pos_int"])
    if qual in _KIND_BY_QUALNAME:
        kind = _KIND_BY_QUALNAME[qual]
        return (kind, *_FIXTURES[kind])
    enum_cls = getattr(coercer, "__self__", None)  # a StrEnum's bound .parse classmethod
    if enum_cls is not None:
        members = [member.value for member in enum_cls]
        return ("enum", members[:2], ["__definitely_not_a_valid_member__"])
    msg = f"no parity fixture kind for {key} ({qual})"
    raise AssertionError(msg)


def _outcome(fn: Any, value: Any) -> tuple[bool, Any]:
    """(raised, result) for calling *fn* on *value* — the accept/value contract."""
    try:
        return (False, fn(value))
    except Exception:  # noqa: BLE001 — parity compares raise-vs-accept, not the exception class
        return (True, None)


class TestParityMatrix:
    """The model-derived validator matches the storage coercer key-for-key, value-for-value."""

    @pytest.mark.parametrize("key", _KEYS)
    def test_schema_validator_matches_storage_coercer(self, key: str) -> None:
        _kind, accepted, rejected = _fixture_and_valid(key)
        coercer = _STORAGE_COERCER[key]
        adapter = _field_adapter(key)
        for value in [*accepted, *rejected]:
            reg_raised, reg_value = _outcome(coercer, value)
            schema_raised, schema_value = _outcome(adapter.validate_python, value)
            assert reg_raised == schema_raised, f"{key}: raise mismatch on {value!r}"
            if not reg_raised:
                assert schema_value == reg_value, f"{key}: value mismatch on {value!r}"
