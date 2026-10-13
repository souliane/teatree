"""Import row classification through the public importer."""

from django.test import TestCase

from teatree.core.config_interchange.migration import import_toml_to_db
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import WITHHELD_NAME_SCOPE
from tests._send_gate import TEST_TERM_REGISTRY

_SCAN = tuple(term for terms in TEST_TERM_REGISTRY.values() for term in terms)
_OV = TEST_TERM_REGISTRY["overlay"][0]


class TestImportRows(TestCase):
    def test_the_rows_under_a_term_carrying_scope_are_refused_once_without_the_name(self) -> None:
        result = import_toml_to_db(f'[overlays.{_OV}]\nmerge_wip = 4\nwip = "slow"\n', scan_terms=_SCAN)

        assert [(row.scope, row.key, row.reason) for row in result.rejected] == [
            (WITHHELD_NAME_SCOPE, "2 row(s)", "secret (banned-term)")
        ]
        assert ConfigSetting.objects.count() == 0

    def test_a_restore_keeps_those_rows_and_withholds_only_the_name(self) -> None:
        text = f"[backup]\ninclude_private = true\n[overlays.{_OV}]\nmerge_wip = 4\n"

        [row] = import_toml_to_db(text, scan_terms=_SCAN, restore_private=True).written

        assert (row.scope, row.shown_scope, row.toml_value) == (_OV, WITHHELD_NAME_SCOPE, "4")
        assert ConfigSetting.objects.get_effective("merge_wip", scope=_OV) == 4
