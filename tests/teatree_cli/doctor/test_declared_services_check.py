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
        patch.dict(SERVICE_CLIENTS, {Service.SLACK: slack}),
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


def _per_overlay(overlays: dict[str, _Overlay], build):
    slack = ServiceClient(Service.SLACK, build, "Slack messaging backend")
    return (
        patch("teatree.mcp.service_resolver.get_all_overlays", return_value=overlays),
        patch.dict(SERVICE_CLIENTS, {Service.SLACK: slack}),
    )


def test_a_declarer_without_its_own_client_is_warned_when_another_serves_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlays = {"acme": _Overlay(Service.SLACK), "widget": _Overlay(Service.SLACK)}
    overlays_patch, clients_patch = _per_overlay(overlays, lambda name: object() if name == "acme" else None)
    with overlays_patch, clients_patch:
        assert _check_declared_services_configured() is True

    out = capsys.readouterr().out
    assert out.startswith("WARN  slack is served to widget from another declaring overlay's Slack messaging backend")


def test_a_client_builder_that_raises_is_a_fail_not_a_crash(capsys: pytest.CaptureFixture[str]) -> None:
    def _broken(_name: str) -> object:
        message = "pass store locked"
        raise RuntimeError(message)

    overlays_patch, clients_patch = _per_overlay({"acme": _Overlay(Service.SLACK)}, _broken)
    with overlays_patch, clients_patch:
        assert _check_declared_services_configured() is False

    out = capsys.readouterr().out
    assert out.startswith("FAIL  slack is declared by acme")
    assert "RuntimeError: pass store locked" in out
