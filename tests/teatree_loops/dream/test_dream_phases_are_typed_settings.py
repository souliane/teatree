"""Every dream phase toggle is a declared setting, not a sub-key of a colliding container.

The toggles lived in ``ConfigSetting["loops"]["dream"]``, and ``loops`` is ALSO the loop
SEED table — ``[loops.dream]`` in ``defaults.toml`` is the dream ``Loop`` row's own
definition. One key, two schemas: a stored seed table answers ``.get("cross_link")`` with
``None``, so every toggle fell to its hardcoded default and the operator's only path to
them was an env var.

Typed settings give them a home that cannot collide, a shipped default the settings page
renders, and the same env override they already had. The defaults are held EQUAL to the
values the hardcoded seam resolved, so retyping changes no behaviour — that equality is
what this file pins. ``dream_memory_promote`` is the one exception, and it follows the
seam's own later default: batching bounds a pass to one ticket, so it ships ON.
"""

import django.test
import pytest

from teatree.config import UserSettings, get_effective_settings
from teatree.config.schema import TeatreeSettingsSchema
from teatree.core.models import ConfigSetting
from teatree.loops.dream import loop as dream_loop
from teatree.loops.dream import pass_config

#: Each phase's reader paired with the setting that now answers it, and the value the
#: pre-retyping seam resolved to.
PHASES = (
    ("dream_propose_evals", dream_loop.propose_evals_enabled, True),
    ("dream_cross_link", dream_loop.cross_link_enabled, True),
    ("dream_merge", dream_loop.merge_enabled, True),
    ("dream_reindex", dream_loop.reindex_enabled, True),
    ("dream_decay", dream_loop.decay_enabled, True),
    ("dream_compliance_measure", dream_loop.compliance_measure_enabled, True),
    ("dream_memory_promote", dream_loop.memory_promote_enabled, True),
    ("dream_derive_evals", dream_loop.derive_evals_enabled, False),
    ("dream_compliance_escalate", dream_loop.compliance_escalate_enabled, False),
    ("dream_automation_asks", dream_loop.automation_asks_enabled, False),
    ("dream_validate_live", pass_config.validate_live_enabled, False),
)


@pytest.mark.parametrize("phase", PHASES, ids=[key for key, _, _ in PHASES])
def test_each_phase_toggle_is_a_declared_setting(phase: tuple) -> None:
    key = phase[0]
    assert key in TeatreeSettingsSchema.model_fields
    assert hasattr(UserSettings(), key)


@pytest.mark.parametrize("phase", PHASES, ids=[key for key, _, _ in PHASES])
def test_the_shipped_default_is_the_value_the_old_seam_resolved(phase: tuple) -> None:
    key, _reader, shipped = phase
    assert getattr(UserSettings(), key) is shipped


def test_the_retired_promotion_cap_is_no_longer_a_setting() -> None:
    assert "dream_promotion_cap" not in TeatreeSettingsSchema.model_fields


class TestThePhaseReadersConsultTheStore(django.test.TestCase):
    """The point of the retyping: a stored row finally reaches the phase that reads it."""

    def test_a_stored_row_flips_every_phase(self) -> None:
        for key, reader, shipped in PHASES:
            with self.subTest(phase=key):
                ConfigSetting.objects.set_value(key, value=not shipped)
                assert reader() is (not shipped)


class TestTheMeteredValidatorStaysOffUntilAskedFor(django.test.TestCase):
    """Without the validator every clearing candidate is WITHHELD, so OFF is the safe ship."""

    def test_it_ships_off(self) -> None:
        assert UserSettings().dream_validate_live is False
        assert get_effective_settings().dream_validate_live is False


class TestTheEnvLayerStillWinsOverTheStore(django.test.TestCase):
    """The ``T3_DREAM_*`` overrides survive the retyping — they are now the ordinary env tier."""

    def test_a_falsy_env_beats_a_stored_true(self) -> None:
        ConfigSetting.objects.set_value("dream_cross_link", value=True)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("T3_DREAM_CROSS_LINK", "0")
            assert dream_loop.cross_link_enabled() is False

    def test_a_truthy_env_beats_a_stored_false(self) -> None:
        ConfigSetting.objects.set_value("dream_derive_evals", value=False)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("T3_DREAM_DERIVE_EVALS", "1")
            assert dream_loop.derive_evals_enabled() is True


class TestAReadFailureNeverStopsThePass:
    """A settings read that RAISES falls back to the shipped default, loudly, never up."""

    def test_a_phase_falls_back_to_its_shipped_default(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "teatree.config.get_effective_settings",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db blip")),
            )
            assert dream_loop.cross_link_enabled() is True
            assert dream_loop.derive_evals_enabled() is False

    def test_a_ticket_filing_phase_fails_closed_when_the_read_fails(self) -> None:
        """A stored ``false`` cannot be read either, so the shipped ``true`` must not promote in its place."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "teatree.config.get_effective_settings",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db blip")),
            )
            assert dream_loop.memory_promote_enabled() is False
