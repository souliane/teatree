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


class TestExplicitCreatePolicy:
    """``create`` is opt-in: it authorises auto-create on colleague repos too."""

    def test_create_on_colleague_repo_creates(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "create"\n')
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.CREATE

    def test_create_still_prefers_found_existing(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "create"\n')
        assert (
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=True)
            is MissingIssueVerdict.USE_EXISTING
        )


class TestExplicitDummyPolicy:
    """``dummy`` is opt-in: it authorises a placeholder ref on colleague repos too."""

    def test_dummy_on_colleague_repo_uses_dummy(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "dummy"\n')
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.DUMMY

    def test_dummy_still_prefers_found_existing(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "dummy"\n')
        assert (
            resolve_missing_issue_verdict(colleague_facing=True, existing_found=True)
            is MissingIssueVerdict.USE_EXISTING
        )


class TestPerOverlayOverride:
    def test_per_overlay_override_wins_over_global(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trusted overlay can opt into ``create`` without flipping the global."""
        _write(
            config_file,
            "[teatree]\n"
            'missing_issue_ref_policy = "find_existing_then_ask"\n'
            "[overlays.trusted]\n"
            'overlay_class = "x.Y"\n'
            'missing_issue_ref_policy = "create"\n',
        )
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.CREATE


class TestEnvOverride:
    def test_env_var_wins_over_global(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "find_existing_then_ask"\n')
        monkeypatch.setenv("T3_MISSING_ISSUE_POLICY", "dummy")
        assert resolve_missing_issue_verdict(colleague_facing=True, existing_found=False) is MissingIssueVerdict.DUMMY


class TestInvalidValue:
    def test_typo_raises_loud(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nmissing_issue_ref_policy = "auto_create"\n')
        with pytest.raises(ValueError, match="missing_issue_ref_policy"):
            load_config()


class TestRegistryMembership:
    """The setting is opted into the per-overlay and env override tiers."""

    def test_in_overlay_overridable_registry(self) -> None:
        assert "missing_issue_ref_policy" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_in_env_override_registry(self) -> None:
        assert ENV_SETTING_OVERRIDES["T3_MISSING_ISSUE_POLICY"][0] == "missing_issue_ref_policy"
