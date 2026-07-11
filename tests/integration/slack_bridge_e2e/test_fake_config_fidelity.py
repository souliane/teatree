"""Guard the ``_FakeUserSettings`` test double against ``UserSettings`` drift.

The fortress fakes ``UserSettings`` via ``conftest._FakeUserSettings`` so the
backend factory can resolve identities / voice-classifier mode without a real
config store. ``get_effective_settings`` rebuilds the settings with
``dataclasses.replace(base, **layered)`` where ``layered`` carries the overlay
CODE-DEFAULT tier — every key in ``PROMOTED_OVERLAY_CODE_DEFAULT_KEYS`` (#36).
``replace`` re-invokes ``base.__class__(**changes)``, so a promoted key the fake
does not declare raises ``TypeError`` and reds the fortress.

That crash is invisible to CI purely by accident of the checkout directory
name: the code-default tier is populated only when the active overlay resolves,
and active-overlay resolution folds the *cwd basename* onto the ``t3-teatree``
entry point (``discovery._match_canonical_ep`` — the ``-teatree`` suffix rule).
CI runs from ``/app`` (basename ``app`` — no fold → no active overlay → empty
code defaults → ``replace`` never sees the promoted keys), so the fortress stays
green there while a dev clone named ``teatree`` folds, populates the tier, and
goes red. These guards assert the fake/real-settings contract DIRECTLY so the
drift is caught deterministically in every environment, cwd-independent.
"""

from dataclasses import fields, replace

import pytest

from teatree.config.overlay_code_defaults import PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
from teatree.config.settings import UserSettings
from tests.integration.slack_bridge_e2e.conftest import _FakeUserSettings

pytestmark = pytest.mark.integration


class TestFakeUserSettingsFidelity:
    """``_FakeUserSettings`` must stay a faithful structural subset of ``UserSettings``."""

    def test_declares_every_promoted_overlay_code_default_key(self) -> None:
        """RED if a promoted code-default key is missing from the fake.

        The exact drift #3115 introduced: promoting ``dogfood_smoke_skill`` (and
        the six other constants) to the overlay code-default tier without adding
        them to the fake. ``get_effective_settings`` then feeds every promoted key
        to ``replace(_FakeUserSettings(), ...)`` and the missing field raises.
        """
        fake_field_names = {f.name for f in fields(_FakeUserSettings)}
        missing = PROMOTED_OVERLAY_CODE_DEFAULT_KEYS - fake_field_names
        assert not missing, f"_FakeUserSettings is missing promoted code-default fields: {sorted(missing)}"

    def test_is_a_structural_subset_of_real_user_settings(self) -> None:
        """RED if the fake grows a field the real ``UserSettings`` does not have.

        A fake that mirrors non-existent settings is not a faithful subset and
        would let a test pass against a field production never carries.
        """
        real_field_names = {f.name for f in fields(UserSettings)}
        fake_field_names = {f.name for f in fields(_FakeUserSettings)}
        extra = fake_field_names - real_field_names
        assert not extra, f"_FakeUserSettings declares fields absent from UserSettings: {sorted(extra)}"

    def test_absorbs_the_full_code_default_layer_via_replace(self) -> None:
        """RED if ``replace(fake, **promoted_defaults)`` raises — the production shape.

        Mirrors ``get_effective_settings``'s ``replace(base, **layered)`` with the
        real defaults for every promoted key, so this guard fails identically to
        the fortress whenever the fake cannot absorb the code-default tier —
        regardless of cwd/overlay resolution.
        """
        real = UserSettings()
        promoted_defaults = {key: getattr(real, key) for key in PROMOTED_OVERLAY_CODE_DEFAULT_KEYS}
        merged = replace(_FakeUserSettings(), **promoted_defaults)
        for key, value in promoted_defaults.items():
            assert getattr(merged, key) == value
