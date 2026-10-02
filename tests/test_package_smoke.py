import re
from pathlib import Path

import django
from django.apps import apps

import teatree
from teatree.core.overlay import OverlayBase, OverlayConnectors, OverlayProvisioning, OverlayReview, OverlayRuntime

BLUEPRINT = Path(__file__).resolve().parents[1] / "BLUEPRINT.md"
QUOTED_VERSION = re.compile(r'`"(\d+)"`')


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


def test_blueprint_quotes_the_live_overlay_api_version() -> None:
    """Every version literal in BLUEPRINT's pin paragraph must equal the live pin.

    A doc quoting a superseded version reads as a breaking change shipped
    unversioned. The pin itself is pinned above; nothing compared it to the prose,
    so a reverted bump could leave the two disagreeing indefinitely.
    """
    paragraphs = [p for p in BLUEPRINT.read_text(encoding="utf-8").split("\n\n") if "__overlay_api_version__" in p]
    assert paragraphs, "BLUEPRINT must document the overlay API version pin"

    quoted = {literal for paragraph in paragraphs for literal in QUOTED_VERSION.findall(paragraph)}
    assert quoted <= {teatree.__overlay_api_version__}, (
        f"BLUEPRINT quotes overlay API version(s) {sorted(quoted)}, "
        f"but the live pin is {teatree.__overlay_api_version__!r}"
    )
