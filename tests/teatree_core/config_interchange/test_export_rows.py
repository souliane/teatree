"""Export row selection through the public TOML writer."""

import tomllib

from django.test import TestCase

from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.models import ConfigSetting
from teatree.hooks.term_match import line_matches
from tests._send_gate import TEST_TERM_REGISTRY

_SCAN = tuple(term for terms in TEST_TERM_REGISTRY.values() for term in terms)
_CARRIER = f"{TEST_TERM_REGISTRY['leak'][0]} desk"


class TestExportRows(TestCase):
    def test_withheld_row_is_named_and_clean_row_survives(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", _CARRIER)
        ConfigSetting.objects.set_value("mode", "auto")

        exported = export_db_to_toml(scan_terms=_SCAN)

        assert [(row.scope, row.key, row.reason) for row in exported.redacted] == [
            ("", "issue_implementer_label", "banned-term")
        ]
        assert not line_matches(exported.toml, _SCAN)
        assert tomllib.loads(exported.toml)["teatree"]["Agents"]["Mode & harness"]["mode"] == "auto"
