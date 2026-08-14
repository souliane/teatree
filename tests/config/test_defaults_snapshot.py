# test-path: cross-cutting
"""``plan_snapshot`` — the DB→file snapshot plan, its exclusions, and its fingerprint.

Pure over injected dicts (no DB), so the direction and the exclusions are pinned
directly: the CURRENT shipped file is the base (a hand edit survives a snapshot run),
a live global row becomes a PROPOSED change, and safety-posture / dark-flag /
workflow-engagement keys are declined whatever the live box says.
"""

import tomllib
from typing import Any

from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.defaults_snapshot import (
    _HEADER,
    WORKFLOW_ENGAGEMENT_KEYS,
    ShippedFile,
    SnapshotChange,
    change_table,
    conservative_keys,
    default_category_keys,
    pinned_fail_closed_keys,
    plan_fingerprint,
    plan_snapshot,
    render_toml,
)
from teatree.config.feature_flags import dark_flags
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.schema import _DEFAULTS_TOML, Category, setting_meta
from teatree.config.setting_groups import grouped_key_order
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.mcp.write_tools import refuse_reason


def _shipped() -> dict[str, Any]:
    """The committed ``[teatree]`` table in the FLAT namespace the planner works in."""
    return flatten_settings_table(tomllib.loads(_DEFAULTS_TOML.read_text())["teatree"])


def _emitted(text: str) -> dict[str, Any]:
    """A rendered document's ``[teatree]`` table, flattened back to the flat namespace."""
    return flatten_settings_table(tomllib.loads(text)["teatree"])


def _never_banned(_text: str) -> str | None:
    return None


def _plan(live_global: dict[str, Any], *, shipped=None, banned_scan=_never_banned, overlay_rows=None, base=None):
    table = _shipped() if shipped is None else shipped
    return plan_snapshot(
        shipped=ShippedFile(
            table=table,
            text=_DEFAULTS_TOML.read_text(encoding="utf-8") if base is None else base,
            _code_defaults=_shipped(),
        ),
        live_global=live_global,
        overlay_scope_rows=overlay_rows or [],
        banned_scan=banned_scan,
    )


class TestDirectionIsDbIntoTheFile:
    def test_a_live_global_row_becomes_a_proposed_change(self) -> None:
        current = _shipped()["provision_ram_ceiling_percent"]
        plan = _plan({"provision_ram_ceiling_percent": current - 10})
        assert plan.changes == (
            SnapshotChange(
                key="provision_ram_ceiling_percent",
                shipped=current,
                proposed=current - 10,
                scope="global",
            ),
        )
        assert _emitted(plan.toml)["provision_ram_ceiling_percent"] == current - 10

    def test_no_live_rows_proposes_nothing_and_reproduces_the_file(self) -> None:
        plan = _plan({})
        assert plan.changes == ()
        assert _emitted(plan.toml) == _shipped()

    def test_a_hand_edited_value_survives_a_snapshot_run(self) -> None:
        # The file is the BASE, not an output of the in-code defaults: a key the operator
        # hand-edited and the live box does not override keeps the hand-edited value.
        hand_edited = {**_shipped(), "session_stale_after_hours": 24}
        plan = _plan({}, shipped=hand_edited)
        assert plan.changes == ()
        assert _emitted(plan.toml)["session_stale_after_hours"] == 24

    def test_a_live_row_equal_to_the_shipped_value_is_not_a_change(self) -> None:
        current = _shipped()["provision_ram_ceiling_percent"]
        assert _plan({"provision_ram_ceiling_percent": current}).changes == ()

    def test_a_key_absent_from_the_file_is_proposed_at_its_code_default(self) -> None:
        without = {k: v for k, v in _shipped().items() if k != "provision_ram_ceiling_percent"}
        plan = _plan({}, shipped=without)
        assert [(c.key, c.proposed, c.scope) for c in plan.changes] == [
            ("provision_ram_ceiling_percent", _shipped()["provision_ram_ceiling_percent"], "code-default")
        ]
        assert plan.changes[0].shipped is None


class TestNeverMovedThroughThisPath:
    def test_safety_posture_live_override_is_declined(self) -> None:
        # The live box moved the key AWAY from what ships; the snapshot declines either
        # direction — a safety-posture key never travels this path.
        plan = _plan({"autonomy": "babysit"})
        assert plan.changes == ()
        assert _emitted(plan.toml)["autonomy"] == _shipped()["autonomy"]
        assert {d.key: d.reason for d in plan.declined}["autonomy"] == "safety-posture"

    def test_dark_flag_live_override_is_declined(self) -> None:
        plan = _plan({"outer_loop_enabled": True})
        assert plan.changes == ()
        assert _emitted(plan.toml)["outer_loop_enabled"] is False
        assert {d.key: d.reason for d in plan.declined}["outer_loop_enabled"] == "feature-flag"

    def test_every_safety_and_dark_key_is_declined_even_when_the_live_box_moved_it(self) -> None:
        moved = dict.fromkeys(SAFETY_POSTURE_KEYS | frozenset(dark_flags()), "__moved__")
        plan = _plan(moved)
        assert plan.changes == ()
        emitted = _emitted(plan.toml)
        shipped = _shipped()
        for key in moved:
            assert emitted[key] == shipped[key]

    def test_the_master_fail_open_switch_is_declined(self) -> None:
        # The documented self-rescue (`t3 review gate fail-open enable`) leaves a live
        # `danger_gate_fail_open = true` row behind. It is Category.DEFAULT and sits in
        # neither SAFETY_POSTURE_KEYS nor `dark_flags()`, so the planner used to offer the
        # master fail-open switch as a shipped default — inside a multi-key diff the owner
        # approves in one answer.
        plan = _plan({"danger_gate_fail_open": True})
        assert plan.changes == ()
        assert _emitted(plan.toml)["danger_gate_fail_open"] is False
        assert {d.key: d.reason for d in plan.declined}["danger_gate_fail_open"] == "cold-read"

    def test_a_cold_hook_gate_kill_switch_is_declined(self) -> None:
        plan = _plan({"banned_terms_gate_enabled": False})
        assert plan.changes == ()
        assert _emitted(plan.toml)["banned_terms_gate_enabled"] is True
        assert {d.key: d.reason for d in plan.declined}["banned_terms_gate_enabled"] == "cold-hook-gate"

    def test_a_warm_gate_kill_switch_is_declined(self) -> None:
        plan = _plan({"e2e_mandatory_gate_enabled": False, "require_human_approval_to_merge": False})
        assert plan.changes == ()
        emitted, shipped = _emitted(plan.toml), _shipped()
        assert emitted["e2e_mandatory_gate_enabled"] == shipped["e2e_mandatory_gate_enabled"]
        assert emitted["require_human_approval_to_merge"] == shipped["require_human_approval_to_merge"]
        assert {d.key: d.reason for d in plan.declined}["e2e_mandatory_gate_enabled"] == "safety-gate"

    def test_every_key_the_mcp_surface_refuses_is_unmovable_here_too(self) -> None:
        # One idea on two surfaces: a key too dangerous for an agent to flip over MCP is
        # too dangerous to bake into every fresh install. `config` sits below `mcp` and
        # cannot import it, so the lane lists are held equal HERE rather than by an import
        # — an MCP refusal lane this set does not cover turns this red.
        refused = {key for key in ALL_KNOWN_CONFIG_SETTINGS if refuse_reason(key)}
        assert refused
        assert refused <= pinned_fail_closed_keys()

    def test_workflow_engagement_override_is_declined(self) -> None:
        plan = _plan({"mode": "auto", "wip": "full", "issue_implementer_enabled": True})
        assert plan.changes == ()
        assert {d.key for d in plan.declined} >= {"mode", "wip", "issue_implementer_enabled"}

    def test_conservative_keys_is_the_pinned_set_plus_workflow(self) -> None:
        assert conservative_keys() == pinned_fail_closed_keys() | WORKFLOW_ENGAGEMENT_KEYS

    def test_the_five_owner_named_keys_are_all_conservative(self) -> None:
        assert {"wip", "mode", "autoload", "contribute"} <= WORKFLOW_ENGAGEMENT_KEYS

    def test_a_banned_value_is_declined_and_never_reaches_the_file(self) -> None:
        plan = _plan(
            {"colleague_repo_url_pattern": "brandx-corp/*"},
            banned_scan=lambda text: "brandx" if "brandx" in text else None,
        )
        assert plan.changes == ()
        assert {d.key: d.reason for d in plan.declined}["colleague_repo_url_pattern"] == "banned-term:brandx"

    def test_a_malformed_live_value_is_declined_never_fatal(self) -> None:
        plan = _plan({"issue_implementer_max_concurrent": []})
        assert plan.changes == ()
        assert {d.key: d.reason for d in plan.declined}["issue_implementer_max_concurrent"] == "uncoercible"


class TestNonDefaultRowsAreReportedNeverEmitted:
    def test_secret_row_is_skipped_and_reported(self) -> None:
        plan = _plan({"banned_terms": ["acme-bank"]})
        assert "banned_terms" not in _emitted(plan.toml)
        assert "banned_terms" in plan.skipped_secret

    def test_personal_row_is_skipped_and_reported(self) -> None:
        plan = _plan({"slack_user_id": "U123"})
        assert "slack_user_id" not in _emitted(plan.toml)
        assert "slack_user_id" in plan.skipped_personal

    def test_stale_unknown_key_is_reported(self) -> None:
        assert "issue_implementer_require_label" in _plan({"issue_implementer_require_label": True}).stale_keys

    def test_overlay_scope_rows_are_reported_sorted(self) -> None:
        plan = _plan({}, overlay_rows=[("t3-teatree", "autonomy"), ("t3-teatree", "agent_harness")])
        assert plan.overlay_scope_rows == (("t3-teatree", "agent_harness"), ("t3-teatree", "autonomy"))


class TestSerialisedShape:
    def test_default_category_keys_is_exactly_what_the_file_carries(self) -> None:
        assert default_category_keys() == frozenset(_shipped())

    def test_render_toml_round_trips_a_table_through_the_canonical_shape(self) -> None:
        assert _emitted(render_toml(_shipped())) == _shipped()

    def test_a_sibling_seed_table_survives_a_re_render(self) -> None:
        # The generator used to build a FRESH document holding only `[teatree]`, so the
        # first snapshot run would silently delete the loop/mode/schedule seed tables that
        # now live beside it.
        base = '[teatree]\nmode = "interactive"\n\n[loops.inbox]\ndelay_seconds = 60\n'
        rendered = render_toml({**_shipped(), "mode": "auto"}, base_text=base)
        assert tomllib.loads(rendered)["loops"] == {"inbox": {"delay_seconds": 60}}
        assert _emitted(rendered)["mode"] == "auto"

    def test_without_a_base_the_render_carries_only_the_teatree_table(self) -> None:
        # Control for the case above: with no base text there is nothing to preserve, so a
        # surviving sibling table could only have come from the base — the assertion is not
        # passing on ambient file content.
        assert "loops" not in tomllib.loads(render_toml(_shipped()))

    def test_a_snapshot_preserves_every_shipped_seed_table(self) -> None:
        document = tomllib.loads(_plan({}).toml)
        assert set(document) == {"teatree", "loops", "modes", "schedules"}

    def test_file_carries_exactly_the_default_category_keys(self) -> None:
        emitted = _emitted(_plan({}).toml)
        assert set(emitted) == set(_shipped())
        for key in emitted:
            assert setting_meta(key).category is Category.DEFAULT

    def test_a_snapshot_with_no_live_rows_reproduces_the_committed_file_byte_for_byte(self) -> None:
        # The committed file is a fixed point of the renderer, so a proposal's diff shows
        # only real value changes — never a re-serialisation churn the owner must read past.
        assert _plan({}).toml == _DEFAULTS_TOML.read_text(encoding="utf-8")

    def test_the_committed_file_opens_with_the_renderers_header(self) -> None:
        # A snapshot over the existing file preserves the file's OWN header, so `_HEADER`
        # is only reached when the file is absent — and would silently rot. Pinning the two
        # in lockstep keeps a from-scratch write documenting the same tables.
        assert _DEFAULTS_TOML.read_text(encoding="utf-8").startswith(_HEADER)

    def test_the_header_invites_a_hand_edit_rather_than_forbidding_one(self) -> None:
        header = _plan({}).toml.split("\n\n")[0].lower()
        assert "hand-editable" in header
        assert "do not hand-edit" not in header


class TestTheRenderedBlockIsNested:
    """The writer emits the nested shape, so an approved snapshot never re-flattens the file."""

    def _block(self, text: str) -> str:
        """The ``[teatree.*]`` region — from its first group table to the sibling seed tables."""
        section = text[text.index("\n[teatree.") :]
        end = section.find("\n[loops.")
        return section if end < 0 else section[:end]

    def _key_order(self, text: str) -> tuple[str, ...]:
        return tuple(line.split(" =")[0] for line in self._block(text).splitlines() if " = " in line)

    def test_the_rendered_keys_follow_the_group_walk_not_the_alphabet(self) -> None:
        order = self._key_order(render_toml(_shipped()))
        assert order == grouped_key_order(order)
        assert order != tuple(sorted(order)), "the renderer emitted one flat alphabetical wall"

    def test_each_level_of_the_hierarchy_is_a_real_table_path(self) -> None:
        headers = [line for line in self._block(render_toml(_shipped())).splitlines() if line.startswith("[")]
        assert headers[0] == '[teatree.Workspace."Engagement & identity"]'
        assert max(header.count(".") for header in headers) >= 3, headers

    def test_a_key_the_file_is_missing_is_restored_under_its_own_group_table(self) -> None:
        # The one path that INSERTS a key: a DEFAULT key absent from the file falls back to
        # its code default, and must land in its group rather than appended at the bottom.
        without = {key: value for key, value in _shipped().items() if key != "merge_wip"}
        block = self._block(_plan({}, shipped=without).toml)
        cadence = block.index('[teatree.Loops."Cadence & throughput"]')
        assert cadence < block.index("\nmerge_wip = ") < block.index("[teatree.Loops.Scanners]")

    def test_a_genuine_sub_table_setting_keeps_its_own_top_level_path(self) -> None:
        # ``speak`` is a declared setting whose value IS a table, not a group wrapper, so it
        # must stay reachable at ``[teatree.speak]`` rather than sink into its group. Its
        # header carries the key's help text as the same trailing comment every other key's
        # line carries, so the path is read off the line with that comment removed.
        headers = {line.split(" #")[0] for line in render_toml(_shipped()).splitlines()}
        assert "[teatree.speak]" in headers

    def test_nesting_the_block_moved_no_value(self) -> None:
        assert _emitted(render_toml(_shipped())) == _shipped()


class TestFingerprintBindsAnApprovalToOneDiff:
    def test_the_same_change_set_fingerprints_identically(self) -> None:
        current = _shipped()["provision_ram_ceiling_percent"]
        first, second = (_plan({"provision_ram_ceiling_percent": current - 10}) for _ in range(2))
        assert plan_fingerprint(first.changes) == plan_fingerprint(second.changes)

    def test_a_different_proposed_value_fingerprints_differently(self) -> None:
        current = _shipped()["provision_ram_ceiling_percent"]
        one = _plan({"provision_ram_ceiling_percent": current - 10})
        other = _plan({"provision_ram_ceiling_percent": current - 20})
        assert plan_fingerprint(one.changes) != plan_fingerprint(other.changes)

    def test_an_empty_change_set_has_a_fingerprint_too(self) -> None:
        assert plan_fingerprint(()) != ""


class TestChangeTable:
    def test_rows_carry_key_current_proposed_and_scope(self) -> None:
        current = _shipped()["provision_ram_ceiling_percent"]
        headers, rows = change_table(_plan({"provision_ram_ceiling_percent": current - 10}).changes)
        assert headers == ["setting", "shipped now", "proposed", "scope"]
        assert rows == [["provision_ram_ceiling_percent", str(current), str(current - 10), "global"]]

    def test_an_absent_shipped_value_renders_as_absent(self) -> None:
        without = {k: v for k, v in _shipped().items() if k != "provision_ram_ceiling_percent"}
        _headers, rows = change_table(_plan({}, shipped=without).changes)
        assert rows[0][1] == "(absent)"
