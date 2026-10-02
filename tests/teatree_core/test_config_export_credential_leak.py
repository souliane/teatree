"""``config_setting export`` withholds credential coordinates + personal identifiers (F2).

Before the fix the export secret guard only checked ``SECRET_SETTINGS`` (a hand-kept
denylist) plus a banned-term scan, so pass-store credential coordinates
(``anthropic_oauth_pass_paths``, ``*_credential_entry``, …) and personal identifiers
(``slack_user_id``, …) — none listed, none brand-tainted — shipped by DEFAULT into the
"shareable" export. The guard now shares the dashboard's credential classifier plus an
explicit personal-identifier list, so those keys are redacted on export.

All values here are SYNTHETIC placeholders — never a real token/path/handle.
"""

import tomllib
from typing import TYPE_CHECKING

from django.test import TestCase

from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.secret_settings import is_credential_reference
from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.config_interchange.secret_guard import redaction_reason
from teatree.core.models import ConfigSetting, Mode

#: A credential COORDINATE — it names where a secret lives; obviously fake, like every value here.
_SYNTHETIC_COORDINATE = "fake-store/not-a-real-entry"

if TYPE_CHECKING:
    from collections.abc import Sequence

    from teatree.core.config_interchange.secret_guard import RedactedRow


def _teatree(toml: str) -> dict[str, object]:
    """The exported ``[teatree]`` table FLATTENED — the namespace a withhold is judged in.

    The file nests the declaration hierarchy, so a leaked key sits inside a group
    sub-table: asserting ``key not in doc["teatree"]`` against the nested table would
    pass while the key is right there one level down.
    """
    return flatten_settings_table(tomllib.loads(toml).get("teatree", {}))


class TestOneWithholdRuleServesBothDirections(TestCase):
    """The rule the export applies on the way OUT is the one the import applies coming IN.

    Two implementations of "must this row never be shared" would let a dump round-trip the
    very data the guard exists to keep out, so both directions ask this one function.
    """

    def test_each_withhold_class_names_itself(self) -> None:
        assert redaction_reason("anthropic_oauth_pass_paths", ["synthetic/entry"], ()) == "credential-coordinate"
        assert redaction_reason("slack_user_id", "synthetic-user-ref", ()) == "personal-identifier"
        assert redaction_reason("mode", "auto", ()) is None

    def test_a_value_carrying_a_scanned_term_is_withheld_whatever_the_key(self) -> None:
        assert redaction_reason("mode", "acme-internal", ("acme-internal",)) == "banned-term:acme-internal"


class TestExportWithholdsCredentialsAndPersonalIds(TestCase):
    def _export(self) -> object:
        # scan_terms=() isolates the credential/personal withhold from the banned-term path.
        return export_db_to_toml(scan_terms=())

    def _reason_for(self, redacted: "Sequence[RedactedRow]", key: str) -> str | None:
        return next((row.reason for row in redacted if row.key == key), None)

    def test_credential_coordinate_keys_are_withheld(self) -> None:
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["synthetic/oauth-entry"])
        ConfigSetting.objects.set_value("anthropic_api_key_pass_paths", ["synthetic/api-entry"])
        ConfigSetting.objects.set_value("openai_compatible_credential_entry", "synthetic/oai-entry")

        result = self._export()
        teatree = _teatree(result.toml)
        for key in ("anthropic_oauth_pass_paths", "anthropic_api_key_pass_paths", "openai_compatible_credential_entry"):
            assert key not in teatree, f"{key} leaked into the shared export"
            assert self._reason_for(result.redacted, key) == "credential-coordinate"

    def test_personal_identifier_keys_are_withheld(self) -> None:
        ConfigSetting.objects.set_value("slack_user_id", "synthetic-user-ref")
        ConfigSetting.objects.set_value("slack_user_channel", "synthetic-channel-ref")

        result = self._export()
        teatree = _teatree(result.toml)
        for key in ("slack_user_id", "slack_user_channel"):
            assert key not in teatree, f"{key} leaked into the shared export"
            assert self._reason_for(result.redacted, key) == "personal-identifier"

    def test_include_private_still_exports_everything(self) -> None:
        # The personal-backup escape hatch is unchanged: --include-private keeps the keys.
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["synthetic/oauth-entry"])
        ConfigSetting.objects.set_value("slack_user_id", "synthetic-user-ref")
        result = export_db_to_toml(include_private=True, scan_terms=())
        teatree = _teatree(result.toml)
        assert teatree["anthropic_oauth_pass_paths"] == ["synthetic/oauth-entry"]
        assert teatree["slack_user_id"] == "synthetic-user-ref"
        assert result.redacted == ()

    def test_credential_reference_classifier_matches_coordinate_suffixes(self) -> None:
        # The shared classifier the export withhold-set routes through: a key whose
        # name ends in a credential-coordinate suffix is a credential reference; an
        # ordinary setting is not.
        assert is_credential_reference("anthropic_oauth_pass_paths") is True
        assert is_credential_reference("openai_compatible_credential_entry") is True
        assert is_credential_reference("mode") is False
        assert is_credential_reference("slack_user_id") is False

    def test_ordinary_setting_still_exports(self) -> None:
        # Control: a non-credential, non-personal, non-brand setting is NOT withheld.
        ConfigSetting.objects.set_value("mode", "auto")
        result = self._export()
        assert _teatree(result.toml)["mode"] == "auto"
        assert self._reason_for(result.redacted, "mode") is None


class TestTheGuardReachesInsideAValueNotJustItsKey(TestCase):
    """Three of the four withhold classes read the KEY, and a config value is often a TABLE.

    So the container's own name decides nothing: an ``overlays`` registry row holds overlay
    definitions and a preset's ``entries`` holds setting values, and a coordinate inside either
    is exactly as unshareable as one stored under its own key.
    """

    def _export(self) -> object:
        return export_db_to_toml(scan_terms=())

    def test_a_coordinate_inside_the_overlays_registry_never_reaches_the_dump(self) -> None:
        ConfigSetting.objects.set_value("overlays", {"box": {"gitlab_token_pass_key": _SYNTHETIC_COORDINATE}})
        result = self._export()
        assert _SYNTHETIC_COORDINATE not in result.toml
        assert any(row.reason == "credential-coordinate" for row in result.redacted)

    def test_a_coordinate_inside_a_preset_entry_never_reaches_the_dump(self) -> None:
        Mode.objects.create(name="withheld-probe", entries={"gitlab_token_pass_key": _SYNTHETIC_COORDINATE})
        result = self._export()
        assert _SYNTHETIC_COORDINATE not in result.toml
        assert ("modes.withheld-probe", "entries", "credential-coordinate") in [
            (row.scope, row.key, row.reason) for row in result.redacted
        ]

    def test_the_withheld_seed_field_is_dropped_whole_so_an_import_never_deletes_the_rest(self) -> None:
        Mode.objects.create(
            name="withheld-probe",
            description="a preset the guard trims",
            entries={"gitlab_token_pass_key": _SYNTHETIC_COORDINATE},
        )
        emitted = tomllib.loads(self._export().toml)["modes"]["withheld-probe"]
        assert "entries" not in emitted
        assert emitted["description"] == "a preset the guard trims"

    def test_include_private_still_carries_the_seed_field(self) -> None:
        Mode.objects.create(name="withheld-probe", entries={"gitlab_token_pass_key": _SYNTHETIC_COORDINATE})
        result = export_db_to_toml(include_private=True, scan_terms=())
        assert tomllib.loads(result.toml)["modes"]["withheld-probe"]["entries"] == {
            "gitlab_token_pass_key": _SYNTHETIC_COORDINATE
        }
        assert result.redacted == ()

    def test_a_preset_carrying_no_coordinate_still_exports_its_entries(self) -> None:
        Mode.objects.create(name="ordinary-probe", entries={"merge_wip": True})
        result = self._export()
        assert tomllib.loads(result.toml)["modes"]["ordinary-probe"]["entries"] == {"merge_wip": True}
        assert result.redacted == ()
