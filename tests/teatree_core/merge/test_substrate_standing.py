"""The standing substrate grant as configured — what a question gate may treat as already held."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from teatree.core.merge.substrate_standing import SubstrateStandingAuthorization, configured_substrate_grant
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

OVERLAY = "t3-teatree"
OWNER = "owner:standing"


@contextmanager
def _config(**settings: object) -> Iterator[None]:
    rows = [ConfigSetting.objects.set_value(key, value, scope=OVERLAY) for key, value in settings.items()]
    try:
        yield
    finally:
        for row in rows:
            row.delete()


class TestConfiguredSubstrateGrant:
    def test_self_signoff_at_full_autonomy_is_a_grant(self) -> None:
        with _config(autonomy="full", substrate_self_signoff=True):
            assert configured_substrate_grant(overlay_name=OVERLAY) == SubstrateStandingAuthorization(self_signoff=True)

    def test_the_configured_delegation_is_a_grant(self) -> None:
        with _config(autonomy="full", substrate_auto_merge_authorized_by=OWNER):
            assert configured_substrate_grant(overlay_name=OVERLAY) == SubstrateStandingAuthorization(
                delegated_by=OWNER
            )

    def test_no_opt_in_is_no_grant(self) -> None:
        with _config(autonomy="full"):
            assert not configured_substrate_grant(overlay_name=OVERLAY)

    def test_self_signoff_below_full_autonomy_is_no_grant(self) -> None:
        with _config(autonomy="notify", substrate_self_signoff=True):
            assert not configured_substrate_grant(overlay_name=OVERLAY)

    def test_an_unresolved_overlay_is_no_grant(self) -> None:
        with _config(autonomy="full", substrate_self_signoff=True):
            assert not configured_substrate_grant(overlay_name="  ")
