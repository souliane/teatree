# test-path: cross-cutting
"""The bootstrap-env-only boundary as a typed registry + fitness function (#1775).

An irreducible set of settings must be readable *before* Django (and therefore
the DB) is available — the ENV keys the settings module itself needs to open the
DB — so they can never live in the ``ConfigSetting`` store.
``BOOTSTRAP_ENV_ONLY_SETTINGS`` makes that boundary a first-class typed allowlist
with the machine-checked invariants below.

1.  **Disjoint registries** — a bootstrap key can never also be DB-overridable. The
    two registries must not intersect, or the resolver would try to DB-override a
    setting needed before the DB exists.
2.  **Write-refusal** — ``config_setting set`` refuses a bootstrap key, so an admin
    cannot stash a DB row for an env-only setting.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.homes import BOOTSTRAP_ENV_ONLY_SETTINGS
from teatree.core.models import ConfigSetting


def test_bootstrap_registry_is_non_empty() -> None:
    assert BOOTSTRAP_ENV_ONLY_SETTINGS


def test_bootstrap_and_overridable_registries_are_disjoint() -> None:
    overlap = BOOTSTRAP_ENV_ONLY_SETTINGS & set(OVERLAY_OVERRIDABLE_SETTINGS)
    assert overlap == set(), f"bootstrap keys must never be DB-overridable: {overlap}"


def test_known_bootstrap_keys_are_present() -> None:
    assert {"DATABASE_URL", "data_dir", "DJANGO_SETTINGS_MODULE"} == BOOTSTRAP_ENV_ONLY_SETTINGS


class TestBootstrapKeyWriteRefusal(TestCase):
    def test_set_refuses_a_bootstrap_env_only_key(self) -> None:
        for key in BOOTSTRAP_ENV_ONLY_SETTINGS:
            with pytest.raises(SystemExit):
                call_command("config_setting", "set", key, '"x"', stderr=StringIO())
            assert ConfigSetting.objects.filter(key=key).exists() is False
