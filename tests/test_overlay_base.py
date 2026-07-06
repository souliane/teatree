"""Tests for teatree.core.overlay — OverlayBase default method coverage."""

from unittest.mock import MagicMock

from teatree.core.overlay import MergeGuard, OverlayBase, ProvisionStep
from teatree.core.provision.variant import Variant
from teatree.core.runners.base import RunnerBase, RunnerResult


class _MinimalOverlay(OverlayBase):
    """Concrete subclass implementing only the abstract methods."""

    def get_repos(self):
        return ["org/repo-a"]

    def get_provision_steps(self, worktree):
        return [ProvisionStep(name="test", callable=lambda: None)]


def _make_worktree():
    """Return a mock Worktree for passing to overlay methods."""
    return MagicMock()


def test_overlay_config_defaults():
    overlay = _MinimalOverlay()
    assert overlay.config.get_gitlab_username() == ""
    assert overlay.config.get_slack_token() == ""
    assert overlay.config.get_review_channel() == ("", "")
    assert overlay.config.known_variants == []


def test_get_env_extra_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.get_env_extra(_make_worktree()) == {}


def test_resolve_variant_default_returns_bare_variant():
    """The default hook resolves to a bare variant (#1306, PR-27) — the tenant is the name."""
    overlay = _MinimalOverlay()
    assert overlay.resolve_variant("client-a") == Variant.bare("client-a")
    assert overlay.resolve_variant("client-a").canonical_tenant == "client-a"
    assert overlay.resolve_variant("").canonical_tenant == ""


def test_classify_customer_display_impact_default_fails_closed():
    """The default returns True for any diff (#1967).

    An overlay that has not declared its path rules treats every change as
    display-impacting so the mandatory-E2E gate is never silently skipped.
    """
    overlay = _MinimalOverlay()
    assert overlay.classify_customer_display_impact(["app/views.py"]) is True
    assert overlay.classify_customer_display_impact(["README.md"]) is True
    assert overlay.classify_customer_display_impact([]) is True


def test_resolve_variant_supports_alias_mapping_in_override():
    """An overlay can map a child variant to its parent tenant (#1306, PR-27).

    The motivating case: a child variant (e.g. ``client-a-regional``)
    shares snapshots with its parent (``client-a``). Without the alias
    the DSLR lookup tried ``development-client-a-regional`` and found
    nothing; with it the overlay resolves ``canonical_tenant`` to
    ``development-client-a`` and the existing parent snapshot satisfies
    the lookup.
    """

    class _AliasOverlay(_MinimalOverlay):
        def resolve_variant(self, name: str) -> Variant:
            aliases = {"client-a-regional": "client-a"}
            return Variant(name=name, canonical_tenant=f"development-{aliases.get(name, name)}")

    overlay = _AliasOverlay()
    assert overlay.resolve_variant("client-a").canonical_tenant == "development-client-a"
    assert overlay.resolve_variant("client-a-regional").canonical_tenant == "development-client-a"
    assert overlay.resolve_variant("client-b").canonical_tenant == "development-client-b"


def test_get_run_commands_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.get_run_commands(_make_worktree()) == {}


def test_get_test_command_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_test_command(_make_worktree()) == []


def test_get_lint_command_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_lint_command(_make_worktree()) == []


def test_get_e2e_preflight_returns_empty_list_by_default():
    overlay = _MinimalOverlay()
    assert overlay.get_e2e_preflight(customer="acme", base_url="https://dev.example.com") == []
    assert overlay.get_e2e_preflight(customer=None, base_url=None) == []


def test_get_mcp_provider_expectations_default_is_empty():
    """The #2282 hook defaults to ``{}`` — overlay values live in the overlay repo (#251)."""
    overlay = _MinimalOverlay()
    assert overlay.get_mcp_provider_expectations() == {}


def test_get_e2e_scenarios_default_is_empty_tuple():
    """The scenario-manifest seam defaults to ``()`` — overlay scenarios live in the overlay.

    Core reads per-feature E2E scenarios through this overlay-agnostic hook
    (mirroring ``get_e2e_run_provenance``); the default empty tuple keeps an
    overlay that ships no scenario manifest inert, so every registered overlay
    resolves without an override.
    """
    overlay = _MinimalOverlay()
    assert overlay.get_e2e_scenarios("") == ()
    assert overlay.get_e2e_scenarios("e2e/playwright/contrib/x/y.spec.ts") == ()


def test_get_e2e_env_extras_returns_empty_dict_by_default():
    overlay = _MinimalOverlay()
    assert overlay.get_e2e_env_extras({}) == {}
    assert overlay.get_e2e_env_extras({"WT_VARIANT": "acme"}) == {}


def test_get_e2e_playwright_args_returns_empty_list_by_default():
    """No overlay adds Playwright args unless it opts in — keeps the prior command shape."""
    overlay = _MinimalOverlay()
    assert overlay.get_e2e_playwright_args("") == []
    assert overlay.get_e2e_playwright_args("playwright/api-flow/foo.spec.ts") == []


def test_get_db_import_strategy_returns_none():
    overlay = _MinimalOverlay()
    assert overlay.get_db_import_strategy(_make_worktree()) is None


def test_db_import_returns_false():
    overlay = _MinimalOverlay()
    assert overlay.db_import(_make_worktree()) is False


def test_db_import_with_force_returns_false():
    overlay = _MinimalOverlay()
    assert overlay.db_import(_make_worktree(), force=True) is False


def test_get_post_db_steps_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_post_db_steps(_make_worktree()) == []


def test_get_reset_passwords_command_returns_none():
    overlay = _MinimalOverlay()
    assert overlay.get_reset_passwords_command(_make_worktree()) is None


def test_get_symlinks_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_symlinks(_make_worktree()) == []


def test_get_services_config_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.get_services_config(_make_worktree()) == {}


def test_get_base_images_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_base_images(_make_worktree()) == []


def test_get_docker_services_returns_empty_set():
    overlay = _MinimalOverlay()
    assert overlay.get_docker_services(_make_worktree()) == set()


def test_reap_worktree_external_resources_returns_empty_list_by_default():
    """#1523: an overlay with no out-of-band resources opts out via the default."""
    overlay = _MinimalOverlay()
    assert overlay.reap_worktree_external_resources(_make_worktree()) == []


def test_validate_pr_passes_conforming_title_and_what_why_description():
    overlay = _MinimalOverlay()
    result = overlay.metadata.validate_pr(
        "feat(ship): add the gate (#1540)",
        "feat(ship): add the gate (#1540)\n\n## What\nAdds the gate.\n\n## Why\nThe convention is missed.",
    )
    assert result == {"errors": [], "warnings": []}


def test_validate_pr_rejects_non_conforming_title_first_line_and_missing_what_why():
    overlay = _MinimalOverlay()
    result = overlay.metadata.validate_pr("Add the gate", "no headers here")
    assert result["warnings"] == []
    assert len(result["errors"]) == 3


def test_get_followup_repos_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_followup_repos() == []


def test_get_skill_metadata_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_skill_metadata() == {}


def test_get_ci_project_path_returns_empty_string():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_ci_project_path() == ""


def test_get_e2e_config_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_e2e_config() == {}


def test_detect_variant_returns_empty_string():
    overlay = _MinimalOverlay()
    assert overlay.metadata.detect_variant() == ""


def test_can_auto_merge_default_is_permissive():
    """Default can_auto_merge returns an allowing MergeGuard from the canonical surface."""
    overlay = _MinimalOverlay()
    guard = overlay.can_auto_merge(target_ref="main", thread_ref="thread-1")
    assert isinstance(guard, MergeGuard)
    assert guard.allowed is True
    assert guard.reason == ""
    assert guard.escalate is False


def test_get_workspace_repos_delegates_to_get_repos():
    overlay = _MinimalOverlay()
    assert overlay.get_workspace_repos() == ["org/repo-a"]


def test_get_pre_run_steps_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_pre_run_steps(_make_worktree(), "frontend") == []


def test_get_tool_commands_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_tool_commands() == []


def test_abstract_methods_implemented():
    overlay = _MinimalOverlay()
    wt = _make_worktree()
    assert overlay.get_repos() == ["org/repo-a"]
    steps = overlay.get_provision_steps(wt)
    assert len(steps) == 1
    assert steps[0].name == "test"


def test_runner_result_defaults():
    r = RunnerResult(ok=True)
    assert r.ok is True
    assert r.detail == ""


def test_runner_result_with_detail():
    r = RunnerResult(ok=False, detail="something broke")
    assert r.ok is False
    assert r.detail == "something broke"


def test_runner_base_run_raises_not_implemented():
    import pytest  # noqa: PLC0415

    class _CallsSuper(RunnerBase):
        def run(self):
            return super().run()

    with pytest.raises(NotImplementedError):
        _CallsSuper().run()


def test_get_repos_raises_not_implemented():
    """OverlayBase.get_repos() raises NotImplementedError when called directly."""
    import pytest  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        OverlayBase.get_repos(MagicMock())


def test_get_provision_steps_raises_not_implemented():
    """OverlayBase.get_provision_steps() raises NotImplementedError when called directly."""
    import pytest  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        OverlayBase.get_provision_steps(MagicMock(), _make_worktree())
