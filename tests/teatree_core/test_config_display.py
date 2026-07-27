"""The shared config-display helpers both dash config surfaces consume (cluster 9)."""

from teatree.core.config_display import MASKED, is_secret, render_value


class TestIsSecret:
    """One taxonomy — the full four-class union of what must never render."""

    def test_secret_category_and_denylist_keys_are_secret(self) -> None:
        assert is_secret("banned_terms") is True  # Category.SECRET + SECRET_SETTINGS
        assert is_secret("github_token_pass_key") is True  # credential coordinate

    def test_a_personal_identifier_not_on_the_denylist_is_secret(self) -> None:
        # slack_user_id is a personal identifier only — NOT in SECRET_SETTINGS. This is the
        # drift the two surfaces diverged on: the config surface used to leave it unmasked.
        assert is_secret("slack_user_id") is True

    def test_an_ordinary_dial_is_not_secret(self) -> None:
        assert is_secret("mode") is False
        assert is_secret("issue_implementer_enabled") is False

    def test_an_unknown_non_schema_key_is_not_secret(self) -> None:
        # A key that is neither a secret/personal/credential coordinate NOR a model field
        # (a stale or bogus key) is safely reported non-secret, never a KeyError.
        assert is_secret("a_removed_or_unknown_key") is False


class TestRenderValue:
    """Booleans as on/off, every empty as a single dash, else the value's text."""

    def test_booleans_render_on_off(self) -> None:
        assert render_value(value=True) == "on"
        assert render_value(value=False) == "off"

    def test_every_empty_renders_a_dash(self) -> None:
        assert render_value(None) == "—"
        assert render_value("") == "—"
        assert render_value([]) == "—"
        assert render_value({}) == "—"

    def test_a_present_value_renders_its_text(self) -> None:
        assert render_value("auto") == "auto"
        assert render_value(4) == "4"

    def test_masked_is_the_value_redaction(self) -> None:
        assert MASKED == "***"
