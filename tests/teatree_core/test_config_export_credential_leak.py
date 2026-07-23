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

from teatree.config.secret_settings import is_credential_reference
from teatree.core.config_migration import RedactedRow, export_db_to_toml
from teatree.core.models import ConfigSetting

if TYPE_CHECKING:
    from collections.abc import Sequence


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
        doc = tomllib.loads(result.toml)
        teatree = doc.get("teatree", {})
        for key in ("anthropic_oauth_pass_paths", "anthropic_api_key_pass_paths", "openai_compatible_credential_entry"):
            assert key not in teatree, f"{key} leaked into the shared export"
            assert self._reason_for(result.redacted, key) == "credential-coordinate"

    def test_personal_identifier_keys_are_withheld(self) -> None:
        ConfigSetting.objects.set_value("slack_user_id", "synthetic-user-ref")
        ConfigSetting.objects.set_value("slack_user_channel", "synthetic-channel-ref")
        ConfigSetting.objects.set_value("availability_schedule", "mon-fri 09:00-17:00")

        result = self._export()
        teatree = tomllib.loads(result.toml).get("teatree", {})
        for key in ("slack_user_id", "slack_user_channel", "availability_schedule"):
            assert key not in teatree, f"{key} leaked into the shared export"
            assert self._reason_for(result.redacted, key) == "personal-identifier"

    def test_include_private_still_exports_everything(self) -> None:
        # The personal-backup escape hatch is unchanged: --include-private keeps the keys.
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["synthetic/oauth-entry"])
        ConfigSetting.objects.set_value("slack_user_id", "synthetic-user-ref")
        result = export_db_to_toml(include_private=True, scan_terms=())
        teatree = tomllib.loads(result.toml)["teatree"]
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
        assert tomllib.loads(result.toml)["teatree"]["mode"] == "auto"
        assert self._reason_for(result.redacted, "mode") is None
