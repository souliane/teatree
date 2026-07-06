"""The :class:`Variant` value object and the ``resolve_variant`` overlay seam."""

import inspect

from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.core.provision.variant import Variant


class _StubOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["repo"]

    def get_provision_steps(self, worktree):
        return []


class _TenantPrefixOverlay(_StubOverlay):
    """An overlay that prefixes the tenant and aliases a child variant."""

    def resolve_variant(self, name: str) -> Variant:
        parent = "client-a" if name == "client-a-regional" else name
        tenant = f"development-{parent}"
        return Variant(
            name=name,
            canonical_tenant=tenant,
            dslr_snapshot_name=f"{tenant}.dump",
            e2e_credentials_key=f"e2e/{tenant}",
        )


def test_bare_variant_is_pass_through():
    variant = Variant.bare("client-a")
    assert variant.name == "client-a"
    assert variant.canonical_tenant == "client-a"
    assert variant.dslr_snapshot_name == ""
    assert variant.e2e_credentials_key == ""


def test_bare_variant_of_empty_name_is_empty_tenant():
    assert Variant.bare("").canonical_tenant == ""


def test_default_resolve_variant_returns_bare_variant():
    overlay = _StubOverlay()
    resolved = overlay.resolve_variant("client-a")
    assert resolved == Variant.bare("client-a")


def test_override_prefixes_tenant_and_aliases_child_variant():
    overlay = _TenantPrefixOverlay()
    assert overlay.resolve_variant("client-a").canonical_tenant == "development-client-a"
    assert overlay.resolve_variant("client-a-regional").canonical_tenant == "development-client-a"
    assert overlay.resolve_variant("client-b").canonical_tenant == "development-client-b"


def test_no_overlay_hook_takes_a_raw_variant_string():
    """No ``OverlayBase`` hook takes a raw ``variant: str`` parameter (PR-27).

    The variant is a first-class :class:`Variant`; a hook that needs one
    resolves it through :meth:`resolve_variant`, so a parameter literally named
    ``variant`` annotated ``str`` on the extension surface is the regression
    this pins against.
    """
    offenders: list[str] = []
    for method_name, method in inspect.getmembers(OverlayBase, predicate=inspect.isfunction):
        signature = inspect.signature(method)
        for param_name, param in signature.parameters.items():
            if param_name == "variant" and param.annotation is str:
                offenders.append(f"OverlayBase.{method_name}({param_name}: str)")
    assert offenders == [], f"overlay hooks still take a raw variant string: {offenders}"


def test_overlay_config_carries_no_raw_variant_tenant_resolver():
    """``OverlayConfig`` exposes no raw-variant tenant resolver (cutover check)."""
    assert not hasattr(OverlayConfig, "get_dslr_tenant_for_variant")
