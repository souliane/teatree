"""Every registry key rides the interchange — derived from ``REGISTRY_KEYS``, never named here.

A registry key is excluded from the ``[teatree]`` table because it is not a ``UserSettings``
field, so if nothing else claims it the export drops it SILENTLY: the dump succeeds, the file
looks complete, and the key is simply gone. That is what a hand-kept emitter list buys.

So the assertions below iterate ``REGISTRY_KEYS`` itself: adding a fourth registry to the
schema and forgetting the interchange turns this file RED rather than shipping a lossy export.
"""

from django.test import TestCase

from teatree.config.registries import REGISTRY_KEYS
from teatree.core.config_interchange.migration import export_db_to_toml, import_toml_to_db
from teatree.core.models import ConfigSetting

_ENTRY = {"url": "https://example.invalid/x", "note": "a peer"}


class TestEveryRegistryKeyRoundTrips(TestCase):
    def _seed(self) -> None:
        for key in REGISTRY_KEYS:
            ConfigSetting.objects.set_value(key, {f"{key}-entry": dict(_ENTRY)})

    def test_the_registry_set_is_more_than_the_two_that_have_always_been_emitted(self) -> None:
        assert set(REGISTRY_KEYS) >= {"overlays", "e2e_repos", "peer_instances"}

    def test_every_registry_key_reaches_the_export(self) -> None:
        self._seed()
        toml = export_db_to_toml(scan_terms=()).toml
        missing = [key for key in REGISTRY_KEYS if f"[{key}." not in toml]
        assert not missing, f"these registry keys were silently dropped from the export: {missing}"

    def test_no_registry_key_is_dumped_under_the_teatree_table(self) -> None:
        self._seed()
        table = export_db_to_toml(scan_terms=()).toml.split("[teatree]", 1)[-1].split("\n[", 1)[0]
        assert not [key for key in REGISTRY_KEYS if f"{key} =" in table]

    def test_every_registry_key_comes_back_through_the_import(self) -> None:
        self._seed()
        toml = export_db_to_toml(scan_terms=()).toml
        ConfigSetting.objects.all().delete()

        result = import_toml_to_db(toml, scan_terms=())

        assert result.rejected == ()
        stored = ConfigSetting.objects.overrides_for_scope("")
        for key in REGISTRY_KEYS:
            if key == "overlays":
                # An overlay's definitions rebuild through its own [overlays.<name>] split.
                continue
            assert stored.get(key) == {f"{key}-entry": _ENTRY}, key

    def test_export_import_export_is_byte_identical_with_every_registry_populated(self) -> None:
        self._seed()
        first = export_db_to_toml(scan_terms=()).toml
        ConfigSetting.objects.all().delete()

        import_toml_to_db(first, scan_terms=())

        assert export_db_to_toml(scan_terms=()).toml == first
