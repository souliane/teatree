"""Loop-start connector preflight gate (refuse-to-continue on down connector).

An overlay that hard-depends on external connectors must HARD-FAIL
(refuse to continue) when one is unreachable, rather than degrade into
silent no-ops. The gate ``raise SystemExit`` with a message naming
WHICH connector is down.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.connector_preflight import run_connector_preflight
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep


class _NoOpOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class _SlackDownOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []

    def get_connector_preflight(self) -> list:
        def _probe() -> None:
            msg = "Slack auth.test failed: missing_scope"
            raise RuntimeError(msg)

        return [_probe]


class TestOverlayBaseConnectorPreflightDefault(TestCase):
    def test_default_is_empty(self) -> None:
        assert _NoOpOverlay().get_connector_preflight() == []


class TestRunConnectorPreflight(TestCase):
    def test_clean_overlays_return_none(self) -> None:
        with patch(
            "teatree.core.connector_preflight.get_all_overlays",
            return_value={"clean": _NoOpOverlay()},
        ):
            assert run_connector_preflight() is None

    def test_down_connector_raises_systemexit_naming_the_connector(self) -> None:
        with (
            patch(
                "teatree.core.connector_preflight.get_all_overlays",
                return_value={"acme": _SlackDownOverlay()},
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            run_connector_preflight()

        assert excinfo.value.code != 0
        message = str(excinfo.value)
        assert "acme" in message
        assert "Slack" in message
        assert "missing_scope" in message

    def test_named_overlay_filter_skips_other_overlays(self) -> None:
        with patch(
            "teatree.core.connector_preflight.get_all_overlays",
            return_value={"clean": _NoOpOverlay(), "acme": _SlackDownOverlay()},
        ):
            # Restricting to the clean overlay must not trip the down one.
            assert run_connector_preflight("clean") is None

    def test_named_overlay_filter_still_gates_the_selected_overlay(self) -> None:
        with (
            patch(
                "teatree.core.connector_preflight.get_all_overlays",
                return_value={"clean": _NoOpOverlay(), "acme": _SlackDownOverlay()},
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            run_connector_preflight("acme")

        assert excinfo.value.code != 0

    def test_unknown_named_overlay_is_a_clean_noop(self) -> None:
        with patch(
            "teatree.core.connector_preflight.get_all_overlays",
            return_value={"acme": _SlackDownOverlay()},
        ):
            assert run_connector_preflight("does-not-exist") is None
