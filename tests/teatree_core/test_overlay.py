import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, OverlayConfig, ProvisionStep
from teatree.core.overlay_loader import get_all_overlay_names, get_overlay, reset_overlay_cache


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


class TestGetOverlay(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

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

    def test_uses_env_var_when_multiple_overlays(self) -> None:
        overlay_a = DummyOverlay()
        self._monkeypatch.setenv("T3_OVERLAY_NAME", "a")
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"a": overlay_a, "b": DummyOverlay()},
        ):
            assert get_overlay() is overlay_a

    def test_env_var_ignored_when_explicit_name(self) -> None:
        overlay_b = DummyOverlay()
        self._monkeypatch.setenv("T3_OVERLAY_NAME", "a")
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"a": DummyOverlay(), "b": overlay_b},
        ):
            assert get_overlay("b") is overlay_b


class TestDiscoverOverlaysValidation:
    def test_rejects_entry_point_not_subclassing_overlay_base(self) -> None:
        from teatree.core.overlay_loader import _discover_overlays  # noqa: PLC0415

        fake_ep = type("FakeEP", (), {"name": "bad", "value": "some.module:NotOverlay", "load": lambda self: str})()
        with (
            patch("importlib.metadata.entry_points", return_value=[fake_ep]),
            pytest.raises(ImproperlyConfigured, match="does not subclass OverlayBase"),
        ):
            _discover_overlays.__wrapped__()


class TestOverlayBase(TestCase):
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


class TestGetAllOverlayNames(TestCase):
    def test_includes_entry_point_overlays(self) -> None:
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"ep-overlay": DummyOverlay()},
        ):
            names = get_all_overlay_names()
        assert "ep-overlay" in names

    def test_includes_path_only_toml_overlays(self) -> None:
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"ep-overlay": DummyOverlay()},
            ),
            patch("teatree.config.load_config") as mock_config,
        ):
            mock_config.return_value.raw = {
                "overlays": {
                    "ep-overlay": {},
                    "toml-path-only": {"path": "~/workspace/other"},
                    "toml-config-only": {"github_token_pass_key": "key"},
                },
            }
            names = get_all_overlay_names()
        assert "ep-overlay" in names
        assert "toml-path-only" in names
        assert "toml-config-only" not in names


class TestOverlayConfig(TestCase):
    def test_toml_skips_reserved_keys(self) -> None:
        mock_config = MagicMock()
        mock_config.raw = {
            "overlays": {
                "test-overlay": {
                    "class": "my.overlay.Class",
                    "path": "/some/path",
                    "custom_setting": "value",
                },
            },
        }
        with patch("teatree.config.load_config", return_value=mock_config):
            config = OverlayConfig(overlay_name="test-overlay")
        assert config.custom_setting == "value"
        assert not hasattr(config, "class")  # reserved, skipped

    def test_toml_pass_key_registers_secret_reader(self) -> None:
        mock_config = MagicMock()
        mock_config.raw = {
            "overlays": {
                "test-overlay": {
                    "github_token_pass_key": "my/secret/key",
                },
            },
        }
        with patch("teatree.config.load_config", return_value=mock_config):
            config = OverlayConfig(overlay_name="test-overlay")
        assert hasattr(config, "get_github_token")
        with patch("teatree.utils.secrets.read_pass", return_value="secret-value"):
            assert config.get_github_token() == "secret-value"


class TestDefaultHealthChecks(TestCase):
    def test_includes_worktree_symlink_and_db_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            source = Path(tmp) / "source"
            source.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/1")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="test_db",
                extra={"worktree_path": str(wt_path)},
            )

            overlay = DummyOverlay()
            with patch.object(
                overlay,
                "get_symlinks",
                return_value=[
                    {"path": "link", "source": str(source), "mode": "symlink"},
                ],
            ):
                checks = overlay.get_health_checks(worktree)
            names = [c.name for c in checks]
            assert "worktree-exists" in names
            assert "symlink-link" in names
            assert "db-name-set" in names

    def test_symlink_check_fails_when_source_directory_is_empty(self) -> None:
        """A symlink pointing at an empty source directory must fail the health check.

        Regression guard for t3-o.#55 Bug 1: ``node_modules`` symlinks whose
        main-clone target was an empty directory silently passed health, so
        lifecycle setup reported ``[OK] symlinks`` while every worktree's
        frontend was broken.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            empty_source = Path(tmp) / "empty_source"
            empty_source.mkdir()

            link_dest = wt_path / "node_modules"
            link_dest.symlink_to(empty_source)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/2")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="test_db",
                extra={"worktree_path": str(wt_path)},
            )

            overlay = DummyOverlay()
            with patch.object(
                overlay,
                "get_symlinks",
                return_value=[
                    {"path": "node_modules", "source": str(empty_source), "mode": "symlink"},
                ],
            ):
                checks = overlay.get_health_checks(worktree)
            symlink_check = next(c for c in checks if c.name == "symlink-node_modules")
            assert symlink_check.check() is False

    def test_symlink_check_passes_when_source_directory_is_populated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            populated_source = Path(tmp) / "populated_source"
            populated_source.mkdir()
            (populated_source / "some-package").mkdir()

            link_dest = wt_path / "node_modules"
            link_dest.symlink_to(populated_source)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/3")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="test_db",
                extra={"worktree_path": str(wt_path)},
            )

            overlay = DummyOverlay()
            with patch.object(
                overlay,
                "get_symlinks",
                return_value=[
                    {"path": "node_modules", "source": str(populated_source), "mode": "symlink"},
                ],
            ):
                checks = overlay.get_health_checks(worktree)
            symlink_check = next(c for c in checks if c.name == "symlink-node_modules")
            assert symlink_check.check() is True

    def test_symlink_check_passes_when_dest_is_real_populated_directory(self) -> None:
        """A real populated directory at *dest* must pass even if *source* is empty.

        Regression guard for #480: when ``npm install`` ran directly in the
        worktree (replacing the symlink with a real ``node_modules`` directory)
        and the main-clone source is empty, provision used to fail the health
        check even though packages are installed and the worktree is usable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            empty_source = Path(tmp) / "empty_source"
            empty_source.mkdir()

            real_dest = wt_path / "node_modules"
            real_dest.mkdir()
            (real_dest / "some-package").mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/4")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="test_db",
                extra={"worktree_path": str(wt_path)},
            )

            overlay = DummyOverlay()
            with patch.object(
                overlay,
                "get_symlinks",
                return_value=[
                    {"path": "node_modules", "source": str(empty_source), "mode": "symlink"},
                ],
            ):
                checks = overlay.get_health_checks(worktree)
            symlink_check = next(c for c in checks if c.name == "symlink-node_modules")
            assert symlink_check.check() is True

    def test_symlink_check_fails_when_dest_is_real_empty_directory(self) -> None:
        """A real but empty directory at *dest* must fail the health check."""
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            populated_source = Path(tmp) / "populated_source"
            populated_source.mkdir()
            (populated_source / "some-package").mkdir()

            real_dest = wt_path / "node_modules"
            real_dest.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/5")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="test_db",
                extra={"worktree_path": str(wt_path)},
            )

            overlay = DummyOverlay()
            with patch.object(
                overlay,
                "get_symlinks",
                return_value=[
                    {"path": "node_modules", "source": str(populated_source), "mode": "symlink"},
                ],
            ):
                checks = overlay.get_health_checks(worktree)
            symlink_check = next(c for c in checks if c.name == "symlink-node_modules")
            assert symlink_check.check() is False
