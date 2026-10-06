"""``t3 doctor`` FAILs a third-party service an overlay declares but teatree has no credentials for."""

from unittest.mock import patch

import pytest

from teatree.backends.types import Service
from teatree.cli.doctor.checks_mcp import _check_declared_services_configured
from teatree.core.overlay import OverlayConfig
from teatree.mcp.service_resolver import SERVICE_CLIENTS, ServiceClient


class _Overlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))


def _declared(overlays: dict[str, _Overlay], *, client: object | None):
    slack = ServiceClient(Service.SLACK, lambda _name: client, "Slack messaging backend")
    return (
        patch("teatree.mcp.service_resolver.get_all_overlays", return_value=overlays),
        patch.dict(SERVICE_CLIENTS, {Service.SLACK: slack.resolve}),
    )


def test_a_declared_service_without_credentials_fails_naming_it_and_its_overlay(
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlays_patch, clients_patch = _declared({"acme": _Overlay(Service.SLACK)}, client=None)
    with overlays_patch, clients_patch:
        assert _check_declared_services_configured() is False

    out = capsys.readouterr().out
    assert out.startswith("FAIL  slack is declared by acme")
    assert "Slack messaging backend" in out


def test_a_declared_service_with_a_configured_client_passes(capsys: pytest.CaptureFixture[str]) -> None:
    overlays_patch, clients_patch = _declared({"acme": _Overlay(Service.SLACK)}, client=object())
    with overlays_patch, clients_patch:
        assert _check_declared_services_configured() is True

    assert "FAIL" not in capsys.readouterr().out


def test_no_declared_service_is_a_silent_pass(capsys: pytest.CaptureFixture[str]) -> None:
    overlays_patch, clients_patch = _declared({}, client=None)
    with overlays_patch, clients_patch:
        assert _check_declared_services_configured() is True

    assert capsys.readouterr().out == ""
