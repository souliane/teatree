"""``T3_*`` env-tier coercion, driven through the live resolver.

The env tier is the highest-precedence layer, so its coercers decide what a flag an
operator exported actually resolves to. Integration-first: the assertions go through
``env_setting_overrides``, the function the resolver itself calls, rather than the
parser in isolation.
"""

import pytest

from teatree.config.enums import Mode
from teatree.config.resolution import env_setting_overrides, read_env_setting_overrides
from teatree.config.setting_parsers import _parse_env_bool, _parse_env_bool_default_on, _parse_private_repos


def test_private_repo_suffix_canonicalises_after_case_fold() -> None:
    assert _parse_private_repos(["GitLab.com/Owner/Repo.GIT/"]) == ["gitlab.com/owner/repo"]


class TestEnvBoolRejectsWhatItCannotRead:
    """An unrecognised token RAISES rather than resolving to ``False``.

    Coercing a typo to ``False`` disables whichever control the operator was exporting,
    and the shell reports nothing — the flag simply does not take effect.
    """

    @pytest.mark.parametrize("token", ["1", "true", "TRUE", " yes ", "on"])
    def test_truthy_tokens(self, token: str) -> None:
        assert _parse_env_bool(token) is True

    @pytest.mark.parametrize("token", ["0", "false", "NO", " off "])
    def test_falsy_tokens(self, token: str) -> None:
        assert _parse_env_bool(token) is False

    @pytest.mark.parametrize("token", ["treu", "ture", "enabled", "2", "off!"])  # codespell:ignore ture
    def test_unrecognised_tokens_raise(self, token: str) -> None:
        with pytest.raises(ValueError, match="Invalid boolean env value"):
            _parse_env_bool(token)


class TestEnvBoolDefaultOn:
    """The default-ON sibling is unchanged: only an explicit off-value disables it."""

    @pytest.mark.parametrize(("token", "expected"), [("0", False), ("off", False), ("", True), ("treu", True)])
    def test_only_an_off_value_disables(self, token: str, *, expected: bool) -> None:
        assert _parse_env_bool_default_on(token) is expected


class TestARefusedValueIsCarried:
    """The refusal is REPORTED as well as raised, so a settings surface can name it (#4585).

    `env_setting_overrides` failing loud is right — resolving a misconfigured pin to
    anything at all would run the box on a value nobody chose. But the blast radius
    included the dash settings page, whose entire job is surfacing exactly this
    misconfiguration, and which died on it instead.
    """

    def test_the_read_reports_the_var_its_raw_value_and_the_parser_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_MODE", "1")

        rejection = read_env_setting_overrides().rejected["mode"]

        assert (rejection.env_var, rejection.raw) == ("T3_MODE", "1")
        assert "1" in rejection.message

    def test_the_other_vars_still_parse_alongside_a_refused_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_MODE", "1")
        monkeypatch.setenv("T3_MERGE_WIP", "9")

        read = read_env_setting_overrides()

        assert read.values["merge_wip"] == 9
        assert "mode" not in read.values

    def test_a_clean_environment_reports_no_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_MODE", "auto")

        read = read_env_setting_overrides()

        assert read.rejected == {}
        assert read.values["mode"] is Mode.AUTO

    def test_the_resolver_raises_the_parsers_own_error_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Carrying the refusal must not degrade it into a wrapper: the resolver re-raises
        # the parser's own exception object, so the message an operator sees is unchanged.
        monkeypatch.setenv("T3_MODE", "1")
        carried = read_env_setting_overrides().rejected["mode"].error

        with pytest.raises(ValueError, match="Invalid t3 mode") as raised:
            env_setting_overrides()

        assert type(raised.value) is type(carried)
        assert str(raised.value) == str(carried) == "Invalid t3 mode '1'; valid values: interactive, auto"
