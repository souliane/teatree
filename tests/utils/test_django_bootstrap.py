"""``ensure_django`` — the single sanctioned ``django.setup()`` bootstrap.

The helper consolidates the 30+ inline ``import django`` +
``DJANGO_SETTINGS_MODULE`` setdefault + ``django.setup()`` blocks that had
drifted across the CLI under two private wrapper names. The call-site
authorization itself is pinned by the ``django-setup-bootstrap`` chokepoint
(``tests/quality/test_chokepoints.py``); here we pin the helper's own
contract: it sets the settings module default and is safe to call repeatedly.
"""

import os
from unittest.mock import patch

from teatree.utils.django_bootstrap import ensure_django


class TestEnsureDjango:
    def test_sets_settings_module_default_and_calls_setup(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("django.setup") as setup,
        ):
            os.environ.pop("DJANGO_SETTINGS_MODULE", None)
            ensure_django()
            assert os.environ["DJANGO_SETTINGS_MODULE"] == "teatree.settings"
            setup.assert_called_once_with()

    def test_preserves_an_explicit_settings_module(self) -> None:
        with (
            patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "overlay.settings"}, clear=False),
            patch("django.setup"),
        ):
            ensure_django()
            assert os.environ["DJANGO_SETTINGS_MODULE"] == "overlay.settings"

    def test_idempotent_across_repeated_calls(self) -> None:
        with patch("django.setup") as setup:
            ensure_django()
            ensure_django()
            assert setup.call_count == 2
