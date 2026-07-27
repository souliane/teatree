# test-path: cross-cutting
"""``plan_snapshot`` — the DB→file snapshot plan, its exclusions, and its fingerprint.

Pure over injected dicts (no DB), so the direction and the exclusions are pinned
directly: the CURRENT shipped file is the base (a hand edit survives a snapshot run),
a live global row becomes a PROPOSED change, and safety-posture / dark-flag /
workflow-engagement keys are declined whatever the live box says.
"""

import tomllib
from typing import Any

from teatree.config.defaults_snapshot import (
    WORKFLOW_ENGAGEMENT_KEYS,
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
from teatree.config.schema import _DEFAULTS_TOML, Category, setting_meta
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS


def _shipped() -> dict[str, Any]:
    return tomllib.loads(_DEFAULTS_TOML.read_text())["teatree"]


def _never_banned(_text: str) -> str | None:
    return None


def _plan(live_global: dict[str, Any], *, shipped=None, banned_scan=_never_banned, overlay_rows=None):
    table = _shipped() if shipped is None else shipped
    return plan_snapshot(
        shipped=table,
        code_defaults=_shipped(),
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
        assert tomllib.loads(plan.toml)["teatree"]["provision_ram_ceiling_percent"] == current - 10

    def test_no_live_rows_proposes_nothing_and_reproduces_the_file(self) -> None:
        plan = _plan({})
        assert plan.changes == ()
        assert tomllib.loads(plan.toml)["teatree"] == _shipped()

    def test_a_hand_edited_value_survives_a_snapshot_run(self) -> None:
        # The file is the BASE, not an output of the in-code defaults: a key the operator
        # hand-edited and the live box does not override keeps the hand-edited value.
        hand_edited = {**_shipped(), "session_stale_after_hours": 24}
        plan = _plan({}, shipped=hand_edited)
        assert plan.changes == ()
        assert tomllib.loads(plan.toml)["teatree"]["session_stale_after_hours"] == 24

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
        plan = _plan({"autonomy": "full"})
        assert plan.changes == ()
        assert tomllib.loads(plan.toml)["teatree"]["autonomy"] == "babysit"
        assert {d.key: d.reason for d in plan.declined}["autonomy"] == "safety-posture"

    def test_dark_flag_live_override_is_declined(self) -> None:
        plan = _plan({"directive_loop_enabled": True})
        assert plan.changes == ()
        assert tomllib.loads(plan.toml)["teatree"]["directive_loop_enabled"] is False
        assert {d.key: d.reason for d in plan.declined}["directive_loop_enabled"] == "dark-flag"

    def test_every_safety_and_dark_key_is_declined_even_when_the_live_box_moved_it(self) -> None:
        moved = dict.fromkeys(SAFETY_POSTURE_KEYS | frozenset(dark_flags()), "__moved__")
        plan = _plan(moved)
        assert plan.changes == ()
        emitted = tomllib.loads(plan.toml)["teatree"]
        shipped = _shipped()
        for key in moved:
            assert emitted[key] == shipped[key]

    def test_workflow_engagement_override_is_declined(self) -> None:
        plan = _plan({"mode": "auto", "wip": "full", "issue_implementer_enabled": True})
        assert plan.changes == ()
        assert {d.key for d in plan.declined} >= {"mode", "wip", "issue_implementer_enabled"}

    def test_conservative_keys_is_the_union_of_safety_dark_and_workflow(self) -> None:
        assert conservative_keys() == SAFETY_POSTURE_KEYS | frozenset(dark_flags()) | WORKFLOW_ENGAGEMENT_KEYS

    def test_pinned_fail_closed_keys_is_safety_plus_dark_only(self) -> None:
        # The un-approvable set the gate also refuses — narrower than `conservative_keys`,
        # which additionally declines the workflow keys a maintainer MAY still ship.
        assert pinned_fail_closed_keys() == SAFETY_POSTURE_KEYS | frozenset(dark_flags())
        assert not pinned_fail_closed_keys() & WORKFLOW_ENGAGEMENT_KEYS

    def test_the_five_owner_named_keys_are_all_conservative(self) -> None:
        assert {"wip", "mode", "autoload", "contribute", "agent_runtime"} <= WORKFLOW_ENGAGEMENT_KEYS

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
        assert "banned_terms" not in tomllib.loads(plan.toml)["teatree"]
        assert "banned_terms" in plan.skipped_secret

    def test_personal_row_is_skipped_and_reported(self) -> None:
        plan = _plan({"slack_user_id": "U123"})
        assert "slack_user_id" not in tomllib.loads(plan.toml)["teatree"]
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
        assert tomllib.loads(render_toml(_shipped()))["teatree"] == _shipped()

    def test_file_carries_exactly_the_default_category_keys(self) -> None:
        emitted = tomllib.loads(_plan({}).toml)["teatree"]
        assert set(emitted) == set(_shipped())
        for key in emitted:
            assert setting_meta(key).category is Category.DEFAULT

    def test_a_snapshot_with_no_live_rows_reproduces_the_committed_file_byte_for_byte(self) -> None:
        # The committed file is a fixed point of the renderer, so a proposal's diff shows
        # only real value changes — never a re-serialisation churn the owner must read past.
        assert _plan({}).toml == _DEFAULTS_TOML.read_text(encoding="utf-8")

    def test_the_header_invites_a_hand_edit_rather_than_forbidding_one(self) -> None:
        header = _plan({}).toml.split("\n\n")[0].lower()
        assert "hand-editable" in header
        assert "do not hand-edit" not in header


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
