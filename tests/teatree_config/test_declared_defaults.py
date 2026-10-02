"""``defaults.toml`` is what the declarations render — and the render never reads the file.

The byte-for-byte pin is the CI gate the ``generate_defaults_toml`` hook fixes. The
planted-value test beside it is what makes that pin mean something: a generator sourced
from ``schema.shipped_defaults`` (which is CONSTRUCTED from the file) passes the same pin
while proving nothing, because it re-renders whatever the file already said.
"""

import re
from pathlib import Path
from unittest import mock

import pytest

from teatree.config import declared_defaults as module
from teatree.config.cold_defaults import DEFAULTS_TOML
from teatree.config.declared_defaults import declared_defaults, render_defaults_file
from teatree.config.defaults_snapshot import _HEADER, default_category_keys
from teatree.config.mr_reminder import MrReminderConfig
from teatree.config.settings import UserSettings
from teatree.types import SpeakConfig


def _line(text: str, key: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"{key} = "))


class TestTheCommittedFileIsGenerated:
    def test_the_declarations_render_the_committed_file_byte_for_byte(self) -> None:
        committed = DEFAULTS_TOML.read_text(encoding="utf-8")
        assert render_defaults_file(committed) == committed

    def test_a_file_value_that_no_declaration_states_is_rendered_away(self) -> None:
        # The anti-vacuity control, at the SAME float type as the declared 2.0 so the
        # render can only differ on the VALUE, never on how a number is formatted.
        committed = DEFAULTS_TOML.read_text(encoding="utf-8")
        planted = re.sub(
            r"^artifact_idle_days = 2\.0", "artifact_idle_days = 99.0", committed, count=1, flags=re.MULTILINE
        )
        assert planted != committed

        rendered = render_defaults_file(planted)

        assert _line(planted, "artifact_idle_days").startswith("artifact_idle_days = 99.0 ")
        assert _line(rendered, "artifact_idle_days").startswith("artifact_idle_days = 2.0 ")
        assert rendered == committed

    def test_the_committed_header_is_the_one_a_from_scratch_render_writes(self) -> None:
        # Two copies of the header exist — this file's and the renderer's — and only a
        # from-scratch render writes the second, so nothing else would catch them drifting.
        assert DEFAULTS_TOML.read_text(encoding="utf-8").startswith(_HEADER)

    def test_the_hand_maintained_seed_tables_survive_the_render(self) -> None:
        committed = DEFAULTS_TOML.read_text(encoding="utf-8")
        rendered = render_defaults_file(committed)
        for table in ("[loops.inbox]", "[modes.", "[schedules."):
            assert table in rendered


class TestEveryShippedKeyIsDeclared:
    def test_the_rendered_keys_are_exactly_the_default_category(self) -> None:
        assert set(declared_defaults()) == default_category_keys()

    def test_the_four_cold_read_keys_come_from_their_registry_entry(self) -> None:
        # These four have no ``UserSettings`` field, so before their ``COLD_SETTINGS``
        # entries carried defaults the file was their only source.
        defaults = declared_defaults()
        assert defaults["active_loop_schedule"] == ""
        assert defaults["danger_gate_fail_open"] is False
        assert defaults["token_outage_auto_engage"] is False
        assert defaults["token_outage_preset_name"] == ""

    def test_a_default_category_key_no_declaration_states_raises(self) -> None:
        keys = default_category_keys() | {"a_key_nothing_declares"}
        with (
            mock.patch.object(module, "default_category_keys", return_value=keys),
            pytest.raises(KeyError, match="a_key_nothing_declares"),
        ):
            declared_defaults()


class TestStoredForm:
    def test_the_two_structured_settings_render_as_their_stored_dict(self) -> None:
        defaults = declared_defaults()
        assert defaults["speak"] == UserSettings().speak.to_dict()
        assert defaults["mr_reminder"] == UserSettings().mr_reminder.to_dict()

    @pytest.mark.parametrize("value", [True, 3, 2.5, "full", ["a"], SpeakConfig(), MrReminderConfig()])
    def test_every_shape_the_file_carries_is_renderable(self, value: object) -> None:
        assert module._renderable(value) is not None

    def test_a_shape_the_file_has_never_carried_raises_rather_than_rendering_its_repr(self) -> None:
        with pytest.raises(TypeError, match="no stored form for a PosixPath default"):
            module._renderable(Path("/tmp"))
