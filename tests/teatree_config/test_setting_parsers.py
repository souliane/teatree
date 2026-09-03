"""``T3_*`` env-tier coercion, driven through the live resolver.

The env tier is the highest-precedence layer, so its coercers decide what a flag an
operator exported actually resolves to. Integration-first: the assertions go through
``env_setting_overrides``, the function the resolver itself calls, rather than the
parser in isolation.
"""

import pytest

from teatree.config.resolution import env_setting_overrides
from teatree.config.setting_parsers import _parse_env_bool, _parse_env_bool_default_on


class TestEnvBoolRejectsWhatItCannotRead:
    """An unrecognised token RAISES rather than resolving to ``False``.

    Coercing a typo to ``False`` disables whichever control the operator was exporting,
    and the shell reports nothing — the flag simply does not take effect.
    """

    @pytest.mark.parametrize("token", ["1", "true", "TRUE", " yes ", "on"])
    def test_truthy_tokens(self, token: str) -> None:
        assert _parse_env_bool(token) is True

    @pytest.mark.parametrize("token", ["0", "false", "NO", " off ", ""])
    def test_falsy_tokens(self, token: str) -> None:
        assert _parse_env_bool(token) is False

    @pytest.mark.parametrize("token", ["treu", "ture", "enabled", "2", "off!"])
    def test_unrecognised_tokens_raise(self, token: str) -> None:
        with pytest.raises(ValueError, match="Invalid boolean env value"):
            _parse_env_bool(token)

    def test_a_typo_never_silently_disables_a_safety_control(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ENFORCE_REGULATED_PATH", "treu")

        with pytest.raises(ValueError, match="Invalid boolean env value"):
            env_setting_overrides()

    def test_a_spelled_out_value_still_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ENFORCE_REGULATED_PATH", "false")

        assert env_setting_overrides()["enforce_regulated_path"] is False


class TestEnvBoolDefaultOn:
    """The default-ON sibling is unchanged: only an explicit off-value disables it."""

    @pytest.mark.parametrize(("token", "expected"), [("0", False), ("off", False), ("", True), ("treu", True)])
    def test_only_an_off_value_disables(self, token: str, *, expected: bool) -> None:
        assert _parse_env_bool_default_on(token) is expected
