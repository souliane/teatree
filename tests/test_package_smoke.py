import django
from django.apps import apps

import teatree
from teatree.core.overlay import OverlayBase, OverlayConnectors, OverlayProvisioning, OverlayReview, OverlayRuntime


def test_teatree_apps_register() -> None:
    if not apps.ready:
        django.setup()

    core_config = apps.get_app_config("core")
    agents_config = apps.get_app_config("agents")

    assert teatree.__version__ == "0.0.1"
    assert teatree.__overlay_api_version__ == "2"
    assert core_config.name == "teatree.core"
    assert agents_config.name == "teatree.agents"


def test_overlay_api_version_reflects_the_composed_facet_reshape() -> None:
    """The pin must agree with the actual overlay-facing surface (#3157 AH-8).

    The PR-27b (#3067) reshape replaced ``OverlayBase``'s flat method surface with
    composed facets — a breaking overlay-API change. The version pin was reverted
    2→1 while the reshape stayed, so a stale overlay pinned to v1 would pass its
    import assertion yet break at runtime. This locks the value to the reality:
    while the composed facets are present, the pin must be at least v2, so an
    accidental re-revert to "1" fails here instead of silently misleading overlays.
    """
    if not apps.ready:
        django.setup()

    # The composed-facet surface the reshape introduced is present on OverlayBase.
    assert isinstance(OverlayBase.provisioning, OverlayProvisioning)
    assert isinstance(OverlayBase.runtime, OverlayRuntime)
    assert isinstance(OverlayBase.review, OverlayReview)
    assert isinstance(OverlayBase.connectors, OverlayConnectors)
    # So the pin must record that breaking reshape (v2+), never the pre-reshape v1.
    assert int(teatree.__overlay_api_version__) >= 2
