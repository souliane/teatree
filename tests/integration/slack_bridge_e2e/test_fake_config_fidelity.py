"""Guard the fortress config double against ``UserSettings`` drift.

The fortress used to fake ``UserSettings`` with a hand-written mirror dataclass so the
backend factory could resolve identities / voice-classifier mode without a real config
store. That mirror drifted twice — #3115 promoted ``dogfood_smoke_skill`` and six other
constants to the overlay code-default tier, and later ``single_branch_repos`` joined
them — because ``get_effective_settings`` rebuilds settings with
``dataclasses.replace(base, **layered)`` where ``layered`` carries every key in
``PROMOTED_OVERLAY_CODE_DEFAULT_KEYS``, and ``replace`` re-invokes
``base.__class__(**changes)``. A promoted key the mirror had not been taught raised
``TypeError`` and reddened the whole fortress.

That crash was invisible to CI purely by accident of the checkout directory name: the
code-default tier is populated only when the active overlay resolves, and active-overlay
resolution folds the *cwd basename* onto the ``t3-teatree`` entry point
(``discovery._match_canonical_ep`` — the ``-teatree`` suffix rule). CI runs from ``/app``
(basename ``app`` — no fold → no active overlay → empty code defaults → ``replace`` never
sees the promoted keys), so the fortress stayed green there while a dev clone named
``teatree`` folded, populated the tier, and went red.

``conftest.fake_config`` now returns a REAL ``TeaTreeConfig`` carrying a REAL
``UserSettings``, so there is no second field list to fall behind and the drift is
unrepresentable rather than merely guarded. These tests pin that property: they go RED
if anyone reintroduces a stand-in for ``UserSettings`` here.
"""

from dataclasses import replace

import pytest

from teatree.config.overlay_code_defaults import PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
from teatree.config.settings import TeaTreeConfig, UserSettings
from tests.integration.slack_bridge_e2e.conftest import fake_config

pytestmark = pytest.mark.integration


class TestFortressConfigFidelity:
    """The fortress config must BE the production config types, not a mirror of them."""

    def test_is_the_real_config_and_settings_types(self) -> None:
        """RED if a stand-in dataclass is reintroduced for either config type.

        A mirror is what drifted; using the production classes is what makes the
        drift impossible, so the type identity is the thing worth pinning.
        """
        config = fake_config({"overlays": {}})
        assert type(config) is TeaTreeConfig
        assert type(config.user) is UserSettings

    def test_absorbs_the_full_code_default_layer_via_replace(self) -> None:
        """RED if the fortress settings cannot take the overlay code-default tier.

        Mirrors ``get_effective_settings``'s ``replace(base, **layered)`` with the real
        default for every promoted key — the exact call that raised ``TypeError``
        against the old mirror — so this fails cwd- and overlay-resolution-independently.
        """
        promoted_defaults = {key: getattr(UserSettings(), key) for key in PROMOTED_OVERLAY_CODE_DEFAULT_KEYS}
        merged = replace(fake_config({}).user, **promoted_defaults)
        for key, value in promoted_defaults.items():
            assert getattr(merged, key) == value

    def test_carries_the_injected_overlay_registry(self) -> None:
        """RED if the synthetic ``[overlays.*]`` tables stop reaching ``raw``.

        The registry is the ONE genuinely synthetic part of the double — the per-overlay
        token/bot configs a single real config store cannot express — so it must survive.
        """
        assert fake_config({"overlays": {"acme": {"slack_token_ref": "ref-bot"}}}).raw == {
            "overlays": {"acme": {"slack_token_ref": "ref-bot"}}
        }
