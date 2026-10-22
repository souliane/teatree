"""One field declaration feeds every settings surface."""

from dataclasses import dataclass

import pytest
from pydantic import BeforeValidator

from teatree.config.declaration import (
    SettingSpec,
    declared_fields,
    render_declared_keys,
    render_keys_manifest,
    setting,
    validate_manifest_history,
)
from teatree.config.schema import schema_fields_for
from teatree.config.setting_parsers import _parse_strict_int


def test_one_field_declaration_feeds_every_surface() -> None:
    @dataclass
    class Synthetic:
        sample: int = setting(7, spec=SettingSpec(help="synthetic setting", parse=_parse_strict_int))

    row = declared_fields(Synthetic)["sample"]
    assert row.default == 7
    assert row.help == "synthetic setting"
    assert row.parse("8") == 8
    assert row.box_global is False
    assert render_declared_keys(Synthetic) == "sample\n"

    annotation, default = schema_fields_for(Synthetic)["sample"]
    assert default == 7
    assert any(isinstance(part, BeforeValidator) and part.func is _parse_strict_int for part in annotation.__metadata__)


def test_manifest_requires_a_record_for_removed_keys() -> None:
    with pytest.raises(ValueError, match="vanished"):
        render_keys_manifest("old_key\n", {"new_key"})
    assert render_keys_manifest("RETIRED old_key\n", {"new_key"}) == "RETIRED old_key\nnew_key\n"


def test_committed_manifest_entries_need_a_disposition() -> None:
    with pytest.raises(ValueError, match="without a disposition"):
        validate_manifest_history("old_key\n", "new_key\n")
    validate_manifest_history("old_key\n", "RETIRED old_key\nnew_key\n")
    validate_manifest_history("old_key\n", "RENAMED old_key new_key\nnew_key\n")
    with pytest.raises(ValueError, match="historical disposition"):
        validate_manifest_history("RETIRED old_key\n", "new_key\n")
