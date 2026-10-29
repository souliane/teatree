"""Cold-safe classification of values that must never enter an audit record."""

from teatree.config.value_visibility import MASKED, withholds_value


def test_secret_setting_values_are_withheld() -> None:
    assert withholds_value("banned_term_registry", {"leak": ["private"]})
    assert MASKED == "***"


def test_ordinary_setting_values_are_readable() -> None:
    assert not withholds_value("agent_max_turns", 1000)
