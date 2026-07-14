# test-path: cross-cutting
"""The effective trusted-issue-author resolver — the config half of the UNION (#3235).

The issue-implementer intakes an issue on the strength of WHO AUTHORED IT, not a
manually-applied label. The trusted set is a UNION of three sources; this resolver
owns the two that live in config (``user_identity_aliases`` — the owner's own
handles — and the ``trusted_issue_authors`` allowlist). The third source, the
``TrustedIdentity`` rows, is unioned in at the DB-aware
:mod:`teatree.core.review.author_trust` seam, which config may not import.

Fail-closed: an unconfigured deployment resolves to the EMPTY set, so no issue is
ever intaken on author trust until the operator names a trusted author.
"""

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, effective_trusted_issue_authors
from teatree.config.settings import ENV_SETTING_OVERRIDES


class TestEffectiveTrustedIssueAuthors:
    def test_unconfigured_resolves_to_empty_set(self) -> None:
        """Fail-closed default: no aliases, no allowlist, no trusted authors."""
        assert effective_trusted_issue_authors(UserSettings()) == frozenset()

    def test_user_identity_aliases_are_trusted(self) -> None:
        settings = UserSettings(user_identity_aliases=["souliane"])
        assert effective_trusted_issue_authors(settings) == frozenset({"souliane"})

    def test_trusted_issue_authors_allowlist_is_trusted(self) -> None:
        settings = UserSettings(trusted_issue_authors=["adrien-oper"])
        assert effective_trusted_issue_authors(settings) == frozenset({"adrien-oper"})

    def test_the_two_sources_are_unioned_not_overridden(self) -> None:
        settings = UserSettings(
            user_identity_aliases=["souliane", "souliane-bot"],
            trusted_issue_authors=["adrien-oper"],
        )
        assert effective_trusted_issue_authors(settings) == frozenset(
            {
                "souliane",
                "souliane-bot",
                "adrien-oper",
            }
        )

    def test_handles_are_lower_cased_and_stripped(self) -> None:
        """Forge handles are case-insensitive; the trust set normalises so a gate cannot be case-dodged."""
        settings = UserSettings(user_identity_aliases=["  Souliane "], trusted_issue_authors=["Adrien-Oper"])
        assert effective_trusted_issue_authors(settings) == frozenset({"souliane", "adrien-oper"})

    def test_blank_entries_never_enter_the_trusted_set(self) -> None:
        """A blank handle must not become a wildcard — an empty author is untrusted, always."""
        settings = UserSettings(user_identity_aliases=["", "   "], trusted_issue_authors=[""])
        assert effective_trusted_issue_authors(settings) == frozenset()


class TestTrustedIssueAuthorSettingsSurface:
    def test_require_label_defaults_off(self) -> None:
        """The label is NOT required by default — author trust is the intake gate."""
        assert UserSettings().issue_implementer_require_label is False

    def test_trusted_issue_authors_defaults_empty(self) -> None:
        assert UserSettings().trusted_issue_authors == []

    def test_both_settings_are_overlay_overridable(self) -> None:
        assert "trusted_issue_authors" in OVERLAY_OVERRIDABLE_SETTINGS
        assert "issue_implementer_require_label" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_both_settings_have_an_env_override(self) -> None:
        assert ENV_SETTING_OVERRIDES["T3_TRUSTED_ISSUE_AUTHORS"][0] == "trusted_issue_authors"
        assert ENV_SETTING_OVERRIDES["T3_ISSUE_IMPLEMENTER_REQUIRE_LABEL"][0] == "issue_implementer_require_label"
