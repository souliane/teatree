"""Tests for the ``provisioning.docker_services`` → ``provisioning.services_config`` contract."""

from unittest.mock import MagicMock

import pytest

from teatree.core.management.commands.worktree import validate_docker_service_contract
from teatree.core.overlay import OverlayBase, OverlayProvisioning, ProvisionStep


class _BaseOverlay(OverlayBase):
    def get_repos(self):
        return ["org/repo-a"]

    def get_provision_steps(self, worktree):
        return [ProvisionStep(name="noop", callable=lambda: None)]


def test_validate_passes_when_docker_service_is_declared():
    class _Provisioning(OverlayProvisioning):
        def services_config(self, worktree):
            return {"web": {"service": "web"}, "redis": {"service": "redis"}}

        def docker_services(self, worktree):
            return {"web"}

    class _Overlay(_BaseOverlay):
        provisioning = _Provisioning()

    validate_docker_service_contract(_Overlay(), MagicMock())


def test_validate_passes_when_both_empty():
    """Overlays that opt out of the Docker contract pass validation."""
    validate_docker_service_contract(_BaseOverlay(), MagicMock())


def test_validate_rejects_undeclared_docker_service():
    class _Provisioning(OverlayProvisioning):
        def services_config(self, worktree):
            return {"web": {"service": "web"}}

        def docker_services(self, worktree):
            return {"web", "postgres"}

    class _Overlay(_BaseOverlay):
        provisioning = _Provisioning()

    with pytest.raises(RuntimeError) as exc:
        validate_docker_service_contract(_Overlay(), MagicMock())

    assert "postgres" in str(exc.value)
    assert "services_config" in str(exc.value)
