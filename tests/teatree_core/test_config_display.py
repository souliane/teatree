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
    """Booleans as on/off, each empty as ITS OWN word, else the value's text."""

    def test_booleans_render_on_off(self) -> None:
        assert render_value(value=True) == "on"
        assert render_value(value=False) == "off"

    def test_each_empty_reads_as_a_different_thing(self) -> None:
        # #4078: one em-dash for all four made an empty list indistinguishable from an unset
        # value, which is precisely the distinction someone choosing a default needs. The four
        # renderings must be pairwise different — asserted as a set so the test states the
        # PROPERTY rather than freezing four particular strings.
        rendered = [render_value(None), render_value(""), render_value([]), render_value({})]
        assert len(set(rendered)) == 4, rendered

    def test_an_unset_value_still_reads_as_the_dash(self) -> None:
        # `None` keeps the em-dash: it is the one that genuinely means "no value here".
        assert render_value(None) == "—"

    def test_an_empty_collection_says_it_is_empty_rather_than_absent(self) -> None:
        # The owner's report: a `[]` must not read as "unset". Both name their own shape.
        assert "empty" in render_value([])
        assert "list" in render_value([])
        assert "empty" in render_value({})

    def test_an_empty_string_says_it_is_empty_text(self) -> None:
        assert "empty" in render_value("")

    def test_a_present_value_renders_its_text(self) -> None:
        assert render_value("auto") == "auto"
        assert render_value(4) == "4"

    def test_masked_is_the_value_redaction(self) -> None:
        assert MASKED == "***"
