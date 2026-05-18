"""Tests for the auto-compact kill-switch advisory (issue #980).

User report (2026-05-18 PM): ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=25``
was set in ``~/.claude/settings.json``, the user hit 99% of the 1M
context window without auto-compaction firing once, and a wrong-
direction "fix" (``CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000``) shrunk
the effective trigger to ~50k instead of restoring the intended ~250k.

Root cause (decoded from
``$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe``):
``o13`` short-circuits on ``zKH(model) && !oiH(model,
autoCompactWindow)``. For the 1M-capable ``claude-opus-4-7`` with no
explicit window setting, this returns false BEFORE the pct override
is consulted — auto-compaction is silently disabled regardless of
``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``.

The teatree-side workaround per the issue scope (no harness changes)
is an advisory: when the kill-switch shape is detected from the
env-var combo, surface the matching ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``
recommendation so the agent can patch ``~/.claude/settings.json``
itself (the user is in auto mode).
"""

from teatree.core.autocompact_advisory import (
    AutocompactConfig,
    advisory_text,
    has_pct_override,
    kill_switch_trips,
    recommended_env_var,
)


def _config(**overrides: str | None) -> AutocompactConfig:
    """Build an ``AutocompactConfig`` from explicit env values.

    Avoids depending on the test runner's environment, which would
    leak whatever the developer has set in their shell into the test.
    """
    base = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": None,
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": None,
        "DISABLE_COMPACT": None,
        "DISABLE_AUTO_COMPACT": None,
        "CLAUDE_CODE_MODEL": None,
        "ANTHROPIC_MODEL": None,
    }
    base.update(overrides)
    return AutocompactConfig.from_env({k: v for k, v in base.items() if v is not None})


class TestHasPctOverride:
    def test_unset_is_false(self) -> None:
        assert has_pct_override(_config()) is False

    def test_empty_string_is_false(self) -> None:
        assert has_pct_override(_config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="")) is False

    def test_non_numeric_is_false(self) -> None:
        assert has_pct_override(_config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="auto")) is False

    def test_zero_is_false(self) -> None:
        # Harness ``gE8`` requires ``>0 && <=100`` — zero is treated as "default".
        assert has_pct_override(_config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="0")) is False

    def test_above_one_hundred_is_false(self) -> None:
        # ``>100`` exits the override path in the harness's ``BP_``.
        assert has_pct_override(_config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="150")) is False

    def test_user_real_value_is_true(self) -> None:
        # User's actual setting in 2026-05-18 incident: PCT_OVERRIDE=25.
        assert has_pct_override(_config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25")) is True

    def test_boundary_one_hundred_is_true(self) -> None:
        assert has_pct_override(_config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="100")) is True


class TestKillSwitchTripsExactScenario:
    """Pin the exact 2026-05-18 user incident shape (the bug)."""

    def test_user_incident_2026_05_18_trips(self) -> None:
        # PCT_OVERRIDE=25, no AUTO_COMPACT_WINDOW, opus-4-7[1m] → bug.
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            CLAUDE_CODE_MODEL="claude-opus-4-7[1m]",
        )
        assert kill_switch_trips(config) is True


class TestKillSwitchTripsModelMatrix:
    def test_kill_switch_model_normalized_name(self) -> None:
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            CLAUDE_CODE_MODEL="claude-opus-4-7",
        )
        assert kill_switch_trips(config) is True

    def test_kill_switch_model_one_megabyte_suffix(self) -> None:
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            CLAUDE_CODE_MODEL="claude-opus-4-7[1m]",
        )
        assert kill_switch_trips(config) is True

    def test_kill_switch_model_uppercase(self) -> None:
        # Harness's ``CD`` lowercases — mirror.
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            CLAUDE_CODE_MODEL="Claude-Opus-4-7",
        )
        assert kill_switch_trips(config) is True

    def test_non_opus_47_does_not_trip(self) -> None:
        # 200k models (opus-4-5, sonnet-4-x) hit the standard auto path
        # — no kill-switch.
        for model in (
            "claude-opus-4-5",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ):
            config = _config(
                CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
                CLAUDE_CODE_MODEL=model,
            )
            assert kill_switch_trips(config) is False, model

    def test_anthropic_model_fallback_env_var(self) -> None:
        # When the harness sets only ``ANTHROPIC_MODEL`` (older shape),
        # detection should still fire.
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            ANTHROPIC_MODEL="claude-opus-4-7",
        )
        assert kill_switch_trips(config) is True

    def test_no_model_env_does_not_trip(self) -> None:
        # We don't surface advisories blind; if neither env var is set
        # we cannot know the model and stay quiet.
        config = _config(CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25")
        assert kill_switch_trips(config) is False


class TestKillSwitchTripsBypassConditions:
    def test_no_pct_override_does_not_trip(self) -> None:
        # No user-expressed threshold → nothing to silently drop.
        config = _config(CLAUDE_CODE_MODEL="claude-opus-4-7[1m]")
        assert kill_switch_trips(config) is False

    def test_window_already_set_does_not_trip(self) -> None:
        # ``Kr.source`` becomes "env" → harness ``oiH`` returns true →
        # kill-switch bypassed.
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000",
            CLAUDE_CODE_MODEL="claude-opus-4-7[1m]",
        )
        assert kill_switch_trips(config) is False

    def test_disable_compact_does_not_trip(self) -> None:
        # User explicitly disabled compaction — not the bug, not our job.
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            DISABLE_COMPACT="1",
            CLAUDE_CODE_MODEL="claude-opus-4-7[1m]",
        )
        assert kill_switch_trips(config) is False

    def test_disable_auto_compact_does_not_trip(self) -> None:
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            DISABLE_AUTO_COMPACT="true",
            CLAUDE_CODE_MODEL="claude-opus-4-7[1m]",
        )
        assert kill_switch_trips(config) is False


class TestRecommendedEnvVar:
    def test_recommends_one_million_for_opus_4_7(self) -> None:
        name, value = recommended_env_var()
        assert name == "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
        # 1,000,000 — the model's max window. Setting it lower (e.g.
        # 200_000) shrinks the pct threshold to a fraction of that
        # smaller value, which is the user's 2026-05-18 wrong-direction
        # mistake. Pin the recommended value.
        assert value == "1000000"


class TestAdvisoryText:
    def test_no_advisory_when_kill_switch_does_not_trip(self) -> None:
        assert advisory_text(_config()) is None

    def test_advisory_when_kill_switch_trips(self) -> None:
        config = _config(
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="25",
            CLAUDE_CODE_MODEL="claude-opus-4-7[1m]",
        )
        text = advisory_text(config)
        assert text is not None
        # Must name the fix var so the agent can act on it.
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in text
        assert "1000000" in text
        # Must reference the user's actual pct so the advisory is
        # actionable, not generic.
        assert "25" in text
        # Must reference the issue so the trail is followable.
        assert "#980" in text
