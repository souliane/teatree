"""Tests for the missing-issue-reference policy resolver.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched to them.
No mocks — ``load_config`` / ``get_effective_settings`` exercised end-to-end.

The policy decides what teatree does when a commit/MR needs an issue
reference and the agent has none in hand: it always tries to recover the
ORIGINAL existing issue first, and on a colleague-facing repo it never
auto-creates or uses a dummy ref (it asks the user) unless the operator
opted into ``create`` / ``dummy``.
"""

from pathlib import Path

import pytest

from teatree.config import ENV_SETTING_OVERRIDES, OVERLAY_OVERRIDABLE_SETTINGS, MissingIssuePolicy, load_config
from teatree.core.models import ConfigSetting
from teatree.missing_issue_policy import MissingIssueVerdict, resolve_missing_issue_verdict


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def _write(cfg: Path, body: str) -> None:
    cfg.write_text(body, encoding="utf-8")


class TestDefaultPolicy:
    """The default is find-existing-then-ask — never auto-create, never dummy."""

    def test_default_when_no_config_is_find_existing_then_ask(self, config_file: Path) -> None:
        assert load_config().user.missing_issue_ref_policy is MissingIssuePolicy.FIND_EXISTING_THEN_ASK

    def test_default_when_section_present_but_unset(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert load_config().user.missing_issue_ref_policy is MissingIssuePolicy.FIND_EXISTING_THEN_ASK

    def test_default_on_colleague_repo_asks_after_search(self, config_file: Path) -> None:
        # Colleague-facing repo, no existing issue found → must ASK, never create/dummy.
        assert (
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.ASK_USER
        )

    def test_default_on_colleague_repo_uses_found_existing(self, config_file: Path) -> None:
        # The original existing issue was found → always use it, on any repo.
        assert (
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=True)
            is MissingIssueVerdict.USE_EXISTING
        )

    def test_default_on_own_repo_allows_create_after_search(self, config_file: Path) -> None:
        # On the user's OWN repo, creating is allowed even under the default policy.
        assert resolve_missing_issue_verdict(colleague_facing=False, existing_found=False) is MissingIssueVerdict.CREATE

    def test_default_on_own_repo_uses_found_existing(self, config_file: Path) -> None:
        assert (
            resolve_missing_issue_verdict(colleague_facing=False, existing_found=True)
            is MissingIssueVerdict.USE_EXISTING
        )


@pytest.mark.django_db
class TestExplicitCreatePolicy:
    """``create`` is opt-in: it authorises auto-create on colleague repos too.

    ``missing_issue_ref_policy`` is DB-home (#1775): the opt-in value is the
    GLOBAL-scope ``ConfigSetting`` row, not a ``[teatree]`` TOML key (which is
    ignored on read).
    """

    def test_create_on_colleague_repo_creates(self) -> None:
        ConfigSetting.objects.set_value("missing_issue_ref_policy", MissingIssuePolicy.CREATE.value)
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.CREATE

    def test_create_still_prefers_found_existing(self) -> None:
        ConfigSetting.objects.set_value("missing_issue_ref_policy", MissingIssuePolicy.CREATE.value)
        assert (
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=True)
            is MissingIssueVerdict.USE_EXISTING
        )


@pytest.mark.django_db
class TestExplicitDummyPolicy:
    """``dummy`` is opt-in: it authorises a placeholder ref on colleague repos too.

    DB-home (#1775): the opt-in value is the GLOBAL-scope ``ConfigSetting`` row.
    """

    def test_dummy_on_colleague_repo_uses_dummy(self) -> None:
        ConfigSetting.objects.set_value("missing_issue_ref_policy", MissingIssuePolicy.DUMMY.value)
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.DUMMY

    def test_dummy_still_prefers_found_existing(self) -> None:
        ConfigSetting.objects.set_value("missing_issue_ref_policy", MissingIssuePolicy.DUMMY.value)
        assert (
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=True)
            is MissingIssueVerdict.USE_EXISTING
        )


@pytest.mark.django_db
class TestPerOverlayOverride:
    def test_per_overlay_override_wins_over_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trusted overlay can opt into ``create`` without flipping the global.

        DB-home (#1775): both tiers are ``ConfigSetting`` rows — the global value
        is the GLOBAL scope (``""``) and the per-overlay opt-in is the overlay's
        scope, which the resolver layers on top so it wins.
        """
        ConfigSetting.objects.set_value("missing_issue_ref_policy", MissingIssuePolicy.FIND_EXISTING_THEN_ASK.value)
        ConfigSetting.objects.set_value("missing_issue_ref_policy", MissingIssuePolicy.CREATE.value, scope="trusted")
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.CREATE


class TestEnvOverride:
    def test_env_var_wins_over_global(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "find_existing_then_ask"\n')
        monkeypatch.setenv("T3_MISSING_ISSUE_POLICY", "dummy")
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.DUMMY


@pytest.mark.django_db
class TestInvalidValue:
    def test_typo_raises_loud(self) -> None:
        """An invalid stored value is raised LOUD with the key named (#1775).

        ``missing_issue_ref_policy`` is DB-home, so the authoritative value is a
        ``ConfigSetting`` row; the partition coerces stored values at resolve time
        and a per-row parser failure is raised (never swallowed to the default).
        """
        ConfigSetting.objects.set_value("missing_issue_ref_policy", "auto_create")
        with pytest.raises(ValueError, match="missing_issue_ref_policy"):
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=False)


class TestRegistryMembership:
    """The setting is opted into the per-overlay and env override tiers."""

    def test_in_overlay_overridable_registry(self) -> None:
        assert "missing_issue_ref_policy" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_in_env_override_registry(self) -> None:
        assert ENV_SETTING_OVERRIDES["T3_MISSING_ISSUE_POLICY"][0] == "missing_issue_ref_policy"
