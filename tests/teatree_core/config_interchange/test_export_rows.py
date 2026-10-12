"""Export row selection through the public TOML writer."""

import tomllib

from django.test import TestCase

from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.models import ConfigSetting


class TestExportRows(TestCase):
    def test_withheld_row_is_named_and_clean_row_survives(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "synthetic-customer")
        ConfigSetting.objects.set_value("mode", "auto")

        exported = export_db_to_toml(scan_terms=("synthetic-customer",))

        assert [(row.scope, row.key, row.reason) for row in exported.redacted] == [
            ("", "issue_implementer_label", "banned-term")
        ]
        assert "synthetic-customer" not in exported.toml
        assert tomllib.loads(exported.toml)["teatree"]["Agents"]["Mode & harness"]["mode"] == "auto"
