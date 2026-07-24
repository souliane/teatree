# test-path: cross-cutting
"""``generate_defaults`` — the per-key adopt-live vs keep-conservative decision + serialisation.

Pure over injected dicts (no DB), so the O2 policy is pinned directly: safety /
dark / workflow keys keep the in-code default even under a live override; a plain
tunable adopts the live value; a banned-term hit aborts the adoption; SECRET /
PERSONAL / stale / overlay-scope rows are reported, never emitted.
"""

import tomllib
from typing import Any

from teatree.config.defaults_generator import WORKFLOW_ENGAGEMENT_KEYS, generate_defaults
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.schema import _DEFAULTS_TOML, Category, setting_meta


def _baseline() -> dict[str, Any]:
    return tomllib.loads(_DEFAULTS_TOML.read_text())["teatree"]


def _never_banned(_text: str) -> str | None:
    return None


def _generate(live_global: dict[str, Any], *, banned_scan=_never_banned, overlay_rows=None):
    return generate_defaults(
        in_code_defaults=_baseline(),
        live_global=live_global,
        overlay_scope_rows=overlay_rows or [],
        banned_scan=banned_scan,
    )


class TestAdoptLive:
    def test_plain_tunable_adopts_the_live_value(self) -> None:
        base = _baseline()["provision_ram_ceiling_percent"]
        result = _generate({"provision_ram_ceiling_percent": base - 10})
        emitted = tomllib.loads(result.toml)["teatree"]
        assert emitted["provision_ram_ceiling_percent"] == base - 10
        adopted = {a.key for a in result.report.adopted}
        assert "provision_ram_ceiling_percent" in adopted

    def test_live_value_equal_to_default_is_not_reported_as_adopted(self) -> None:
        base = _baseline()["provision_ram_ceiling_percent"]
        result = _generate({"provision_ram_ceiling_percent": base})
        assert {a.key for a in result.report.adopted} == set()


class TestKeepConservative:
    def test_safety_posture_override_is_declined(self) -> None:
        result = _generate({"autonomy": "full"})
        emitted = tomllib.loads(result.toml)["teatree"]
        assert emitted["autonomy"] == _baseline()["autonomy"] == "babysit"
        declined = {a.key: a.disposition for a in result.report.kept_conservative}
        assert declined["autonomy"] == "kept-conservative:safety-posture"

    def test_dark_flag_override_is_declined(self) -> None:
        result = _generate({"directive_loop_enabled": True})
        emitted = tomllib.loads(result.toml)["teatree"]
        assert emitted["directive_loop_enabled"] is False
        assert any(
            a.key == "directive_loop_enabled" and a.disposition == "kept-conservative:dark-flag"
            for a in result.report.kept_conservative
        )

    def test_workflow_engagement_override_is_declined(self) -> None:
        result = _generate({"mode": "auto", "wip": "full", "issue_implementer_enabled": True})
        emitted = tomllib.loads(result.toml)["teatree"]
        assert emitted["mode"] == "interactive"
        assert emitted["wip"] == "medium"
        assert emitted["issue_implementer_enabled"] is False
        declined = {a.key for a in result.report.kept_conservative}
        assert {"mode", "wip", "issue_implementer_enabled"} <= declined

    def test_the_five_owner_named_keys_are_all_conservative(self) -> None:
        assert {"wip", "mode", "autoload", "contribute", "agent_runtime"} <= WORKFLOW_ENGAGEMENT_KEYS


class TestBannedTermAbort:
    def test_a_banned_value_aborts_the_adoption_and_keeps_the_default(self) -> None:
        base = _baseline()["colleague_repo_url_pattern"]
        result = _generate(
            {"colleague_repo_url_pattern": "brandx-corp/*"},
            banned_scan=lambda text: "brandx" if "brandx" in text else None,
        )
        emitted = tomllib.loads(result.toml)["teatree"]
        assert emitted["colleague_repo_url_pattern"] == base
        assert [a.key for a in result.report.banned_aborted] == ["colleague_repo_url_pattern"]


class TestNonDefaultRowsAreReportedNeverEmitted:
    def test_secret_row_is_skipped_and_reported(self) -> None:
        result = _generate({"banned_terms": ["acme-bank"]})
        emitted = tomllib.loads(result.toml)["teatree"]
        assert "banned_terms" not in emitted
        assert "banned_terms" in result.report.skipped_secret

    def test_personal_row_is_skipped_and_reported(self) -> None:
        result = _generate({"slack_user_id": "U123"})
        emitted = tomllib.loads(result.toml)["teatree"]
        assert "slack_user_id" not in emitted
        assert "slack_user_id" in result.report.skipped_personal

    def test_stale_unknown_key_is_reported(self) -> None:
        result = _generate({"issue_implementer_require_label": True})
        assert "issue_implementer_require_label" in result.report.stale_keys

    def test_overlay_scope_rows_are_reported_sorted(self) -> None:
        result = _generate({}, overlay_rows=[("t3-teatree", "autonomy"), ("t3-teatree", "agent_harness")])
        assert result.report.overlay_scope_rows == [
            ("t3-teatree", "agent_harness"),
            ("t3-teatree", "autonomy"),
        ]


class TestSerialisedShape:
    def test_file_carries_exactly_the_default_category_keys(self) -> None:
        emitted = tomllib.loads(_generate({}).toml)["teatree"]
        default_keys = {k for k in ALL_KNOWN_CONFIG_SETTINGS if setting_meta(k).category is Category.DEFAULT}
        assert set(emitted) == default_keys

    def test_no_secret_or_personal_key_is_emitted(self) -> None:
        emitted = tomllib.loads(_generate({}).toml)["teatree"]
        for key in emitted:
            assert setting_meta(key).category is Category.DEFAULT

    def test_generating_with_no_live_overrides_reproduces_the_baseline(self) -> None:
        # The baseline IS the current file's in-code defaults; regenerating with an
        # empty live store must round-trip every value unchanged.
        emitted = tomllib.loads(_generate({}).toml)["teatree"]
        assert emitted == _baseline()
