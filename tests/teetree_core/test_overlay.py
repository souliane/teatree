from collections.abc import Iterator
from typing import cast

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from teetree.core.models import Ticket, Worktree
from teetree.core.overlay import OverlayBase, ProvisionStep
from teetree.core.overlay_loader import get_overlay, reset_overlay_cache


class DummyOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend", "frontend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def mark_ready() -> None:
            facts = cast("dict[str, str]", worktree.extra or {})
            facts["provisioned_by"] = "dummy"
            worktree.extra = facts
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="mark-ready", callable=mark_ready)]


class InvalidOverlay:
    pass


class SuperCallingOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return super().get_repos()

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return super().get_provision_steps(worktree)


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.test_overlay.DummyOverlay")
@pytest.mark.django_db
def test_get_overlay_loads_and_caches_configured_overlay() -> None:
    first = get_overlay()
    second = get_overlay()

    assert first is second
    assert first.get_repos() == ["backend", "frontend"]


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.test_overlay.DummyOverlay")
@pytest.mark.django_db
def test_overlay_optional_hooks_default_to_empty_values() -> None:
    ticket = Ticket.objects.create()
    worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="feature")
    overlay = get_overlay()

    assert overlay.get_env_extra(worktree) == {}
    assert overlay.get_run_commands(worktree) == {}
    assert overlay.get_db_import_strategy(worktree) is None
    assert overlay.get_post_db_steps(worktree) == []
    assert overlay.get_symlinks(worktree) == []
    assert overlay.get_services_config(worktree) == {}
    assert overlay.validate_mr("title", "description") == {"errors": [], "warnings": []}
    assert overlay.get_skill_metadata() == {}


@override_settings(TEATREE_OVERLAY_CLASS="")
def test_get_overlay_requires_explicit_setting() -> None:
    with pytest.raises(ImproperlyConfigured, match="TEATREE_OVERLAY_CLASS"):
        get_overlay()


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.test_overlay.InvalidOverlay")
def test_get_overlay_rejects_non_overlay_classes() -> None:
    with pytest.raises(ImproperlyConfigured, match="must subclass OverlayBase"):
        get_overlay()


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.test_overlay.MissingOverlay")
def test_get_overlay_reports_bad_import_paths() -> None:
    with pytest.raises(ImproperlyConfigured, match="Could not import overlay"):
        get_overlay()


@pytest.mark.django_db
def test_overlay_base_abstract_fallthroughs_raise_not_implemented() -> None:
    overlay = SuperCallingOverlay()
    worktree = Worktree.objects.create(ticket=Ticket.objects.create(), repo_path="/tmp/backend", branch="feature")

    with pytest.raises(NotImplementedError):
        overlay.get_repos()

    with pytest.raises(NotImplementedError):
        overlay.get_provision_steps(worktree)
