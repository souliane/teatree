import django
from django.apps import apps

import teetree


def test_teetree_apps_register() -> None:
    if not apps.ready:
        django.setup()

    core_config = apps.get_app_config("core")
    agents_config = apps.get_app_config("agents")

    assert teetree.__version__ == "0.0.1"
    assert core_config.name == "teetree.core"
    assert agents_config.name == "teetree.agents"
