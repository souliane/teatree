"""``t3 identities {seed,add,list,remove}`` management command (#1773).

Exercises the real DB through ``call_command`` — the seed is idempotent, add
rejects an unknown platform, and remove deletes by ``(platform, handle)``.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting, TrustedIdentity

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestIdentitiesCommand(TestCase):
    def setUp(self) -> None:
        TrustedIdentity.objects.all().delete()

    def test_seed_consolidates_configured_aliases(self) -> None:
        ConfigSetting.objects.set_value("user_identity_aliases", ["souliane", "adrien.cossa"])
        result = call_command("identities", "seed")
        assert result == {"seeded": 2, "created": 2}
        assert TrustedIdentity.objects.trusted_handles() == {"souliane", "adrien.cossa"}

    def test_seed_is_idempotent(self) -> None:
        ConfigSetting.objects.set_value("user_identity_aliases", ["souliane", "adrien.cossa"])
        call_command("identities", "seed")
        result = call_command("identities", "seed")
        assert result == {"seeded": 2, "created": 0}
        assert TrustedIdentity.objects.count() == 2

    def test_add_inserts_and_is_idempotent(self) -> None:
        first = cast("dict[str, object]", call_command("identities", "add", "github", "newhandle"))
        second = cast("dict[str, object]", call_command("identities", "add", "github", "newhandle"))
        assert first["created"] is True
        assert second["created"] is False
        assert TrustedIdentity.objects.filter(platform="github", handle="newhandle").count() == 1

    def test_add_rejects_unknown_platform(self) -> None:
        with pytest.raises(SystemExit):
            call_command("identities", "add", "bitbucket", "someone")
        assert not TrustedIdentity.objects.exists()

    def test_remove_deletes_by_platform_and_handle(self) -> None:
        TrustedIdentity.objects.create(platform="github", handle="souliane")
        result = call_command("identities", "remove", "github", "souliane")
        assert result == {"removed": 1}
        assert not TrustedIdentity.objects.filter(handle="souliane").exists()

    def test_list_returns_rows(self) -> None:
        TrustedIdentity.objects.create(platform="gitlab", handle="adrien.cossa", note="GitLab")
        rows = call_command("identities", "list")
        assert rows == [{"platform": "gitlab", "handle": "adrien.cossa", "note": "GitLab"}]
