"""Effect-based refusal gate for the MCP ``config_setting_set`` surface (F9.1).

The old refuse-list classified safety keys by NAME-GLOB (``*_gate_enabled``,
``require_*``), which MISSED the delegation / authorization / fail-closed-allowlist
fields whose WRITE is itself the authorization — so a shell-denied MCP agent (e.g. the
triage-assessor) could self-grant substrate-merge delegation
(``substrate_auto_merge_authorized_by``) or widen the fail-closed intake allowlist
(``trusted_issue_authors`` / ``send_proxy_allowlist``). The fix refuses by declared
EFFECT via :data:`~teatree.config.SAFETY_POSTURE_KEYS`.

The CONFORMANCE walk here is the load-bearing deliverable: it enumerates every
``UserSettings`` field, and any field matching the delegation/allowlist/authorization
heuristics that is in NEITHER ``SAFETY_POSTURE_KEYS`` NOR the explicit reviewed
``MCP_SETTABLE_OK`` allowlist turns this suite RED. So a future safety-posture field can
never ship silently MCP-settable — the classification MUST be made deliberately.
"""

import dataclasses
import json
from fnmatch import fnmatch
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.config import SAFETY_POSTURE_KEYS, UserSettings
from teatree.core.models import ConfigSetting
from teatree.mcp import build_server, write_tools
from teatree.mcp.write_tools import MCP_SETTABLE_OK, refuse_reason

# The delegation / authorization / fail-closed-boundary NAME shapes. Deliberately BROAD
# (name-shaped) so the walk over-captures — every match must then be EXPLICITLY sorted
# into "refuse (safety posture)" or "allow (reviewed benign)", which is exactly the
# fail-closed forcing function: a new ``*authorized*`` / ``*allowlist*`` / ``*_threshold``
# field cannot slip through unclassified.
_DELEGATION_GLOBS = (
    "*authorized*",
    "*allowlist*",
    "trusted_*",
    "*signoff*",
    "autonomy",
    "*auto_merge*",
    "*auto_actions*",
    "*post_mode",
    "enforce_*",
    "*_threshold",
)

# The specific keys the F9.1 attack turns on: an MCP agent self-granting substrate-merge
# delegation or widening the fail-closed intake / egress allowlist.
_DELEGATION_ATTACK_KEYS = (
    "substrate_auto_merge_authorized_by",
    "substrate_self_signoff",
    "trusted_issue_authors",
    "send_proxy_allowlist",
    "on_behalf_post_mode",
    "autonomy",
)


def _user_settings_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(UserSettings)}


def _delegation_shaped_fields() -> set[str]:
    return {name for name in _user_settings_field_names() if any(fnmatch(name, g) for g in _DELEGATION_GLOBS)}


def _unclassified(safety: frozenset[str], settable_ok: frozenset[str]) -> set[str]:
    """Delegation-shaped fields in NEITHER the refuse set NOR the reviewed allowlist."""
    return {name for name in _delegation_shaped_fields() if name not in safety and name not in settable_ok}


def _payloads(result: Any) -> list[Any]:
    blocks = result[0] if isinstance(result, tuple) else result
    return [json.loads(block.text) for block in blocks if getattr(block, "text", None) is not None]


def _call(tool: str, args: dict[str, Any]) -> Any:
    return _payloads(async_to_sync(build_server().call_tool)(tool, args))[0]


class TestConformanceWalk:
    """The fail-closed classification gate over every UserSettings field."""

    def test_the_heuristic_is_non_vacuous(self) -> None:
        # Anti-vacuity: the walk must actually capture the known attack keys, or the
        # whole gate would pass vacuously by matching nothing.
        shaped = _delegation_shaped_fields()
        for key in _DELEGATION_ATTACK_KEYS:
            assert key in shaped, f"{key} must be delegation-shaped for the walk to guard it"

    def test_every_delegation_shaped_field_is_explicitly_classified(self) -> None:
        unclassified = _unclassified(SAFETY_POSTURE_KEYS, MCP_SETTABLE_OK)
        assert unclassified == set(), (
            "delegation/authorization/allowlist-shaped UserSettings field(s) are classified "
            f"NEITHER as refused (config.SAFETY_POSTURE_KEYS) NOR reviewed-settable "
            f"(write_tools.MCP_SETTABLE_OK): {sorted(unclassified)}. Add each to exactly one — "
            "the refuse set if its write is an authorization/allowlist boundary, else the "
            "reviewed allowlist."
        )

    def test_removing_any_safety_key_turns_the_walk_red(self) -> None:
        # The load-bearing proof: dropping ANY safety-posture key from the refuse set (and
        # it is not in the reviewed allowlist) makes the conformance walk report it
        # unclassified — so the gate genuinely fails CLOSED on an un-refused safety key.
        for key in SAFETY_POSTURE_KEYS:
            weakened = SAFETY_POSTURE_KEYS - {key}
            assert _unclassified(weakened, MCP_SETTABLE_OK) == {key}, (
                f"removing {key!r} from SAFETY_POSTURE_KEYS must surface it as unclassified"
            )

    def test_refuse_and_reviewed_sets_are_disjoint(self) -> None:
        overlap = SAFETY_POSTURE_KEYS & MCP_SETTABLE_OK
        assert overlap == frozenset(), f"a key cannot be both refused and reviewed-settable: {sorted(overlap)}"

    def test_both_sets_are_real_user_settings_fields(self) -> None:
        fields = _user_settings_field_names()
        bogus_safety = sorted(SAFETY_POSTURE_KEYS - fields)
        bogus_ok = sorted(MCP_SETTABLE_OK - fields)
        assert bogus_safety == [], f"SAFETY_POSTURE_KEYS names that are not UserSettings fields: {bogus_safety}"
        assert bogus_ok == [], f"MCP_SETTABLE_OK names that are not UserSettings fields: {bogus_ok}"

    def test_reviewed_allowlist_is_non_vacuous(self) -> None:
        # Every reviewed-settable entry must actually be delegation-shaped — otherwise it
        # would need no carve-out and is dead weight that hides a real classification.
        shaped = _delegation_shaped_fields()
        stray = sorted(MCP_SETTABLE_OK - shaped)
        assert stray == [], f"MCP_SETTABLE_OK entries that are not delegation-shaped (drop them): {stray}"


class TestRefuseReason:
    def test_every_safety_posture_key_is_refused_by_effect(self) -> None:
        for key in SAFETY_POSTURE_KEYS:
            reason = refuse_reason(key)
            assert reason, f"{key} is a safety-posture key but refuse_reason allowed it"

    def test_safety_posture_refusal_names_the_authorization_effect(self) -> None:
        # The refusal message must name the EFFECT (F9.1), not a name-glob, for at least
        # the keys reached via the SAFETY_POSTURE_KEYS clause (not pre-empted by another).
        assert "authorization" in refuse_reason("substrate_auto_merge_authorized_by")
        assert "authorization" in refuse_reason("trusted_issue_authors")

    def test_reviewed_settable_keys_stay_allowed(self) -> None:
        for key in MCP_SETTABLE_OK:
            assert refuse_reason(key) == "", f"{key} is reviewed-settable but was refused"


class TestConfigSettingSetEndToEnd(TestCase):
    """The refusal fires identically through the actual MCP tool call."""

    def test_each_safety_posture_key_is_refused_and_never_written(self) -> None:
        for key in _DELEGATION_ATTACK_KEYS:
            with pytest.raises(Exception, match="refused"):
                _call("config_setting_set", {"key": key, "value": '"attacker"' if "authorized" in key else "true"})
            assert not ConfigSetting.objects.filter(key=key).exists()

    def test_substrate_delegation_grant_over_mcp_is_refused(self) -> None:
        # The precise F9.1 attack: a shell-denied MCP agent tries to name itself the
        # standing substrate-merge authorization. The write is the authorization, so it
        # must be refused and leave no row.
        with pytest.raises(Exception, match="refused"):
            _call("config_setting_set", {"key": "substrate_auto_merge_authorized_by", "value": '"triage-bot"'})
        assert not ConfigSetting.objects.filter(key="substrate_auto_merge_authorized_by").exists()

    def test_reviewed_settable_key_is_written(self) -> None:
        # A delegation-shaped but reviewed-benign tuning knob still goes through.
        result = _call("config_setting_set", {"key": "e2e_confidence_threshold", "value": "60"})
        assert result["ok"] is True
        assert ConfigSetting.objects.get_effective("e2e_confidence_threshold", scope="") == 60

    def test_refuse_reason_is_the_module_gate(self) -> None:
        # Guards against the tool bypassing refuse_reason: the write path must consult it.
        assert write_tools.refuse_reason("send_proxy_allowlist")
