# test-path: cross-cutting
"""The bootstrap-file-only boundary as a typed registry + fitness function (#1775).

#1775 keeps an irreducible set of settings file-only: they must be readable
*before* Django (and therefore the DB) is available, so they can never live in
the ``ConfigSetting`` store. The boundary used to be documented only in a model
docstring; ``BOOTSTRAP_FILE_ONLY_SETTINGS`` makes it a first-class typed
allowlist with the machine-checked invariants below.

The two invariants that keep the boundary honest:

1.  **Disjoint registries** — a bootstrap key can never also be DB-overridable. If a key
    is in ``OVERLAY_OVERRIDABLE_SETTINGS`` it CAN be moved to the DB; if it is in
    ``BOOTSTRAP_FILE_ONLY_SETTINGS`` it must NOT be. The two registries must not
    intersect, or the resolver would try to DB-override a setting needed before
    the DB exists.
2.  **Write-refusal** — ``config-setting set`` refuses a bootstrap key, so an
    admin cannot stash a DB row for a file-only setting.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.settings import BOOTSTRAP_FILE_ONLY_SETTINGS
from teatree.core.models import ConfigSetting


def test_bootstrap_registry_is_non_empty() -> None:
    # The irreducible set is real: DATABASE_URL / data-dir / DJANGO_SETTINGS_MODULE
    # / the offline private_repos allowlist all must stay file-readable pre-Django.
    assert BOOTSTRAP_FILE_ONLY_SETTINGS


def test_bootstrap_and_overridable_registries_are_disjoint() -> None:
    # The fitness function: a file-only bootstrap key can never be DB-overridable.
    # Goes RED the moment a bootstrap key is also added to OVERLAY_OVERRIDABLE_SETTINGS
    # (or vice versa) — the boundary can no longer silently rot.
    overlap = BOOTSTRAP_FILE_ONLY_SETTINGS & set(OVERLAY_OVERRIDABLE_SETTINGS)
    assert overlap == set(), f"bootstrap keys must never be DB-overridable: {overlap}"


def test_known_bootstrap_keys_are_present() -> None:
    # The four #1775 names are the irreducible set the umbrella calls out by name.
    for key in ("DATABASE_URL", "data_dir", "DJANGO_SETTINGS_MODULE", "private_repos"):
        assert key in BOOTSTRAP_FILE_ONLY_SETTINGS


class TestBootstrapKeyWriteRefusal(TestCase):
    def test_set_refuses_a_bootstrap_file_only_key(self) -> None:
        # A bootstrap key is not in OVERLAY_OVERRIDABLE_SETTINGS, so set already
        # refuses it; this pins that the refusal covers EVERY bootstrap key and
        # never writes a row for one.
        for key in BOOTSTRAP_FILE_ONLY_SETTINGS:
            with pytest.raises(SystemExit):
                call_command("config_setting", "set", key, '"x"', stderr=StringIO())
            assert ConfigSetting.objects.filter(key=key).exists() is False
