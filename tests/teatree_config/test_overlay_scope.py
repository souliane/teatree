"""One explanation for unhonourable overlay rows on write and in doctor."""

from teatree.config.overlay_scope import unhonourable_overlay_scope_reason


def test_global_clock_rejects_overlay_scope() -> None:
    assert "one clock per box" in (unhonourable_overlay_scope_reason("loop_cadence_seconds", "demo") or "")


def test_per_overlay_setting_accepts_overlay_scope() -> None:
    assert unhonourable_overlay_scope_reason("agent_max_turns", "demo") is None
