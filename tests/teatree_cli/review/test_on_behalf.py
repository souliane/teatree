"""``on_behalf_gate_active`` resolves the gate; it never degrades to "gate off".

The helper used to wrap its ``teatree.on_behalf_gate`` import in
``except ModuleNotFoundError: return False`` — a safety gate failing OPEN. The
module is a first-party leaf importing only ``enum`` and ``teatree.config``, so
that branch was unreachable; these tests pin that an unimportable-gate world now
surfaces rather than silently un-gating an approve/unapprove.

The second half pins WHOSE mode the gate reads, through ``check_on_behalf`` —
the live path every publishing ``ReviewService`` method calls. A review post
names its target repo in the invocation, so the mode is the owning overlay's;
resolved ambiently it was the invoking cwd's, which on a multi-overlay install
resolves nothing and refused a post the target overlay had pinned ``immediate``.
A target NO single overlay owns is the other direction of the same error, and
stays gated even when the ambient overlay is pinned ``immediate``.
"""

import builtins
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.cli.review.on_behalf import check_on_behalf, on_behalf_gate_active, target_overlay
from teatree.core.models import ConfigSetting
from teatree.core.overlay_loader import OverlayConfigResolver

_GATE_MODULE = "teatree.on_behalf_gate"
_GATE_ENV = ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS")
_BLOCKED = "on-behalf post blocked"


@pytest.fixture
def no_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every env tier that would decide the mode ahead of the config under test."""
    for env in _GATE_ENV:
        monkeypatch.delenv(env, raising=False)


class TestGateResolutionNeverFailsOpen:
    def test_an_unimportable_gate_module_raises_rather_than_reporting_gate_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_import = builtins.__import__

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == _GATE_MODULE:
                detail = f"No module named {name!r}"
                raise ModuleNotFoundError(detail, name=name)
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, _GATE_MODULE, raising=False)
        monkeypatch.setattr(builtins, "__import__", refuse)

        with pytest.raises(ModuleNotFoundError):
            on_behalf_gate_active()


class TestGateResolutionFollowsTheMode(TestCase):
    """``approve`` is a non-draft action: gated under both blocking modes."""

    @pytest.fixture(autouse=True)
    def _env(self, no_gate_env: None) -> None:
        """Bind the shared env-clearing fixture into this ``TestCase``."""

    def test_ask_reports_the_gate_active(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        assert on_behalf_gate_active() is True

    def test_draft_or_ask_reports_the_gate_active(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "draft_or_ask")
        assert on_behalf_gate_active() is True

    def test_immediate_reports_the_gate_inactive(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert on_behalf_gate_active() is False


@contextmanager
def _owned_by(repos: dict[str, list[str]], scopes: dict[str, dict[str, list[str]]] | None = None) -> Iterator[None]:
    """Registry where *repos* is the whole of enumerated ownership and *scopes* the declared groups."""
    declared = scopes or {}
    with (
        patch(
            "teatree.core.overlay_loader._overlay_repo_slugs_for_inference",
            return_value=sorted(repos.items()),
        ),
        patch.object(OverlayConfigResolver, "all_names", return_value=sorted(declared)),
        patch.object(OverlayConfigResolver, "owned_repos", side_effect=lambda name: declared[str(name)]),
    ):
        yield


class TestTheGateReadsTheTargetOverlaysMode(TestCase):
    """A post is judged by the overlay that owns the repo it is addressed to."""

    @pytest.fixture(autouse=True)
    def _env(self, no_gate_env: None) -> None:
        """Bind the shared env-clearing fixture into this ``TestCase``."""

    def test_the_owning_overlay_is_resolved_from_the_target_repo(self) -> None:
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}):
            assert target_overlay("acme-eng/widget") == "acme-overlay"

    def test_an_immediate_owner_proceeds_under_a_blocking_global_default(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}):
            assert check_on_behalf("acme-eng/widget", 1, "approve") == ""

    def test_a_sibling_overlays_blocking_mode_does_not_reach_another_overlays_repo(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask", "other-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"], "other-overlay": ["other-eng/thing"]}):
            assert check_on_behalf("acme-eng/widget", 1, "approve") == ""
            assert _BLOCKED in check_on_behalf("other-eng/thing", 1, "approve")


class TestAnUnownedTargetIsNotTheAmbientOverlaysBusiness(TestCase):
    """A named target no single overlay owns never inherits an ambient ``immediate`` pin."""

    @pytest.fixture(autouse=True)
    def _ambient_is_acme(self, monkeypatch: pytest.MonkeyPatch, no_gate_env: None) -> None:
        """Make ``acme-overlay`` the AMBIENT overlay; each test pins its ``immediate`` mode."""
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme-overlay")

    def test_a_repo_no_overlay_owns_stays_gated(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}):
            assert target_overlay("stranger/repo") is None
            assert _BLOCKED in check_on_behalf("stranger/repo", 1, "approve")

    def test_an_ambiguously_owned_repo_stays_gated(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "two")
        with _owned_by({"acme-overlay": ["acme-eng/widget"], "two": ["acme-eng/widget"]}):
            assert target_overlay("acme-eng/widget") is None
            assert _BLOCKED in check_on_behalf("acme-eng/widget", 1, "approve")

    def test_a_draft_form_action_is_still_exempt_on_an_unowned_target(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}):
            assert check_on_behalf("stranger/repo", 1, "post_draft_note") == ""


class TestTheDeclaredNamespaceAnswersForTheGroup(TestCase):
    """A repo the owning overlay's table never listed still resolves to it — on the right forge."""

    @pytest.fixture(autouse=True)
    def _env(self, no_gate_env: None) -> None:
        """Bind the shared env-clearing fixture into this ``TestCase``."""

    def test_a_repo_outside_the_table_resolves_to_its_group_owner(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        with _owned_by({}, {"acme-overlay": {"gitlab.com": ["acme-eng"]}}):
            assert target_overlay("acme-eng/created-yesterday") == "acme-overlay"
            assert check_on_behalf("acme-eng/created-yesterday", 1, "approve") == ""

    def test_a_namespace_declared_on_another_forge_never_attributes(self) -> None:
        # ``t3 review`` posts to GitLab only, so a github.com declaration cannot
        # lend its ``immediate`` pin to a GitLab target that merely shares the name.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "gh-overlay")
        with _owned_by({}, {"gh-overlay": {"github.com": ["acme-eng"]}}):
            assert target_overlay("acme-eng/widget") is None
            assert _BLOCKED in check_on_behalf("acme-eng/widget", 1, "approve")


@contextmanager
def _env_mode(value: str) -> Iterator[None]:
    """The operator's session-scoped ``T3_ON_BEHALF_POST_MODE`` — the tier above every stored row."""
    with patch.dict(os.environ, {"T3_ON_BEHALF_POST_MODE": value}):
        yield


class TestTheEnvOverrideStillGovernsATargetOverlayRead(TestCase):
    """``T3_ON_BEHALF_POST_MODE`` outranks the target overlay's stored mode, both ways.

    Reading the mode for a NAMED overlay took the resolver branch that drops the
    env layer, which retired both these overrides in silence — an ``ask`` the
    operator set for the session stopped blocking.
    """

    @pytest.fixture(autouse=True)
    def _env(self, no_gate_env: None) -> None:
        """Bind the shared env-clearing fixture into this ``TestCase``."""

    def test_an_ask_override_still_blocks_an_immediate_owner(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}), _env_mode("ask"):
            assert _BLOCKED in check_on_behalf("acme-eng/widget", 1, "approve")

    def test_an_immediate_override_still_proceeds_against_a_blocking_owner(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}), _env_mode("immediate"):
            assert check_on_behalf("acme-eng/widget", 1, "approve") == ""


class TestAnUnownedTargetDropsOnlyThePerOverlayTier(TestCase):
    """Unowned means "no overlay's pin reaches it", NOT "no configuration at all".

    The class above pins the ambient overlay's pin staying out. Forcing the shipped
    ``DRAFT_OR_ASK`` outright is the over-correction that costs the tiers which are
    not any overlay's opinion: the operator's own ``T3_ON_BEHALF_POST_MODE`` and the
    global workspace default. A repo no overlay ENUMERATES is the common case on a
    single-overlay install, so retiring the env tier there retires it nearly
    everywhere — the same silence ``apply_env=True`` exists to prevent.
    """

    @pytest.fixture(autouse=True)
    def _ambient_is_acme(self, monkeypatch: pytest.MonkeyPatch, no_gate_env: None) -> None:
        """Make ``acme-overlay`` the AMBIENT overlay, so its pin is the one that must not leak."""
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme-overlay")

    def test_an_env_immediate_still_proceeds_on_an_unowned_target(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}), _env_mode("immediate"):
            assert check_on_behalf("stranger/repo", 1, "approve") == ""

    def test_a_global_workspace_default_still_governs_an_unowned_target(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}):
            assert check_on_behalf("stranger/repo", 1, "approve") == ""

    def test_an_env_ask_still_outranks_a_global_immediate_on_an_unowned_target(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}), _env_mode("ask"):
            assert _BLOCKED in check_on_behalf("stranger/repo", 1, "approve")

    def test_with_neither_tier_set_an_unowned_target_still_blocks(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", "acme-overlay")
        with _owned_by({"acme-overlay": ["acme-eng/widget"]}):
            assert _BLOCKED in check_on_behalf("stranger/repo", 1, "approve")
