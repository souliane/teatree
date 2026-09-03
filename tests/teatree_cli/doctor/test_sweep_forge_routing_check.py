"""``_check_sweep_repos_resolve_a_forge`` — a swept repo with no declared forge (#72).

The runtime refusal is one ``ScannerError`` per tick, which the dispatcher DMs. This
is the static half: the sweep is a permanent no-op for a repo whose forge nothing
declares, and every other surface reads healthy while it is.
"""

from types import SimpleNamespace
from unittest.mock import patch

import django.test

from teatree.cli.doctor.checks_sweep_forge import _check_sweep_repos_resolve_a_forge

_GITLAB_SLUG = "acme-eng/platform/widget-api"


def _overlay(*, repos: list[str], owned: dict[str, list[str]]) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(owned_repos=owned),
        metadata=SimpleNamespace(get_followup_repos=lambda: repos),
    )


def _registry(overlay: SimpleNamespace):
    return (
        patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
        patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}),
        patch("teatree.core.merge.host_kind.get_all_overlays", return_value={"acme": overlay}),
    )


class TestSweepForgeRoutingCheck(django.test.TestCase):
    def test_a_declared_namespace_passes(self) -> None:
        overlay = _overlay(repos=[_GITLAB_SLUG], owned={"gitlab.com": ["acme-eng"]})
        with _registry(overlay)[0], _registry(overlay)[1], _registry(overlay)[2]:
            assert _check_sweep_repos_resolve_a_forge() is True

    def test_an_undeclared_namespace_fails_and_names_the_repo(self) -> None:
        overlay = _overlay(repos=[_GITLAB_SLUG], owned={"github.com": ["souliane"]})
        with _registry(overlay)[0], _registry(overlay)[1], _registry(overlay)[2]:
            assert _check_sweep_repos_resolve_a_forge() is False

    def test_an_ambiguous_declaration_fails_rather_than_picking_one(self) -> None:
        contested = {"gitlab.com": ["acme-eng"], "github.com": ["acme-eng"]}
        overlay = _overlay(repos=[_GITLAB_SLUG], owned=contested)
        with _registry(overlay)[0], _registry(overlay)[1], _registry(overlay)[2]:
            assert _check_sweep_repos_resolve_a_forge() is False

    def test_a_broken_registry_degrades_to_a_warning_not_a_crash(self) -> None:
        with patch("teatree.core.overlay_loader.get_all_overlays", side_effect=RuntimeError("registry down")):
            assert _check_sweep_repos_resolve_a_forge() is True
