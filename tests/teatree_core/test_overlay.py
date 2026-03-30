from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep
from teatree.core.overlay_loader import get_overlay, reset_overlay_cache


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


class TestGetOverlay:
    @pytest.mark.django_db
    def test_loads_and_caches_configured_overlay(self) -> None:
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": DummyOverlay()},
        ):
            first = get_overlay()
            second = get_overlay()

            assert first is second
            assert first.get_repos() == ["backend", "frontend"]

    def test_requires_at_least_one_overlay(self) -> None:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={}),
            pytest.raises(ImproperlyConfigured, match="No teatree overlays found"),
        ):
            get_overlay()

    def test_raises_for_unknown_name(self) -> None:
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"test": DummyOverlay()},
            ),
            pytest.raises(ImproperlyConfigured, match="Overlay 'unknown' not found"),
        ):
            get_overlay("unknown")

    def test_raises_when_multiple_without_name(self) -> None:
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"a": DummyOverlay(), "b": DummyOverlay()},
            ),
            pytest.raises(ImproperlyConfigured, match="Multiple overlays found"),
        ):
            get_overlay()


class TestDiscoverOverlaysValidation:
    def test_rejects_entry_point_not_subclassing_overlay_base(self) -> None:
        from teatree.core.overlay_loader import _discover_overlays  # noqa: PLC0415

        fake_ep = type("FakeEP", (), {"name": "bad", "value": "some.module:NotOverlay", "load": lambda self: str})()
        with (
            patch("importlib.metadata.entry_points", return_value=[fake_ep]),
            pytest.raises(ImproperlyConfigured, match="does not subclass OverlayBase"),
        ):
            _discover_overlays.__wrapped__()


class TestOverlayBase:
    @pytest.mark.django_db
    def test_optional_hooks_default_to_empty_values(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        worktree = Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/backend", branch="feature")
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": DummyOverlay()},
        ):
            overlay = get_overlay()

            assert overlay.get_env_extra(worktree) == {}
            assert overlay.get_run_commands(worktree) == {}
            assert overlay.get_db_import_strategy(worktree) is None
            assert overlay.get_post_db_steps(worktree) == []
            assert overlay.get_symlinks(worktree) == []
            assert overlay.get_services_config(worktree) == {}
            assert overlay.metadata.validate_mr("title", "description") == {"errors": [], "warnings": []}
            assert overlay.metadata.get_skill_metadata() == {}

    @pytest.mark.django_db
    def test_abstract_fallthroughs_raise_not_implemented(self) -> None:
        overlay = SuperCallingOverlay()
        worktree = Worktree.objects.create(
            ticket=Ticket.objects.create(overlay="test"),
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature",
        )

        with pytest.raises(NotImplementedError):
            overlay.get_repos()

        with pytest.raises(NotImplementedError):
            overlay.get_provision_steps(worktree)
