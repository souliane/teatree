"""Tests for the ``get_docker_services`` → ``get_services_config`` contract."""

from unittest.mock import MagicMock

import pytest

from teatree.core.management.commands.lifecycle import validate_docker_service_contract
from teatree.core.overlay import OverlayBase, ProvisionStep


class _BaseOverlay(OverlayBase):
    def get_repos(self):
        return ["org/repo-a"]

    def get_provision_steps(self, worktree):
        return [ProvisionStep(name="noop", callable=lambda: None)]


def test_validate_passes_when_docker_service_is_declared():
    class _Overlay(_BaseOverlay):
        def get_services_config(self, worktree):
            return {"web": {"service": "web"}, "redis": {"service": "redis"}}

        def get_docker_services(self, worktree):
            return {"web"}

    validate_docker_service_contract(_Overlay(), MagicMock())


def test_validate_passes_when_both_empty():
    """Overlays that opt out of the Docker contract pass validation."""
    validate_docker_service_contract(_BaseOverlay(), MagicMock())


def test_validate_rejects_undeclared_docker_service():
    class _Overlay(_BaseOverlay):
        def get_services_config(self, worktree):
            return {"web": {"service": "web"}}

        def get_docker_services(self, worktree):
            return {"web", "postgres"}

    with pytest.raises(RuntimeError) as exc:
        validate_docker_service_contract(_Overlay(), MagicMock())

    assert "postgres" in str(exc.value)
    assert "get_services_config" in str(exc.value)
