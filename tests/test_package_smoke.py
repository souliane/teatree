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
    assert teatree.__overlay_api_version__ == "1"
    assert core_config.name == "teatree.core"
    assert agents_config.name == "teatree.agents"


def test_overlay_api_version_is_held_at_v1_pre_stable_release() -> None:
    """The pin is deliberately frozen at "1" pre-stable-release (#3157 AH-8).

    The PR-27b (#3067) reshape replaced ``OverlayBase``'s flat method surface with
    composed facets — a breaking overlay-API *surface* change. But every registered
    overlay was migrated onto the composed facets IN LOCKSTEP with that reshape, so
    the reshaped base loads and passes conformance at "1" today (nothing breaks).
    Pre-1.0 the API is explicitly unstable and core + overlays move together, so the
    counter is intentionally held at "1" (an earlier 1→2 bump was reverted in
    ``2aaff7f25`` for exactly this reason). This locks the deliberate policy: the
    composed-facet surface is present AND the pin stays "1", so a re-bump that
    revives the artificial mismatch fails here.
    """
    if not apps.ready:
        django.setup()

    # The composed-facet surface the reshape introduced is present on OverlayBase.
    assert isinstance(OverlayBase.provisioning, OverlayProvisioning)
    assert isinstance(OverlayBase.runtime, OverlayRuntime)
    assert isinstance(OverlayBase.review, OverlayReview)
    assert isinstance(OverlayBase.connectors, OverlayConnectors)
    # ...yet the pin stays frozen at "1" through the pre-stable window (deliberate policy).
    assert teatree.__overlay_api_version__ == "1"
