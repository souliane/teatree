import django
from django.apps import apps

import teatree


def test_teatree_apps_register() -> None:
    if not apps.ready:
        django.setup()

    core_config = apps.get_app_config("core")
    agents_config = apps.get_app_config("agents")

    assert teatree.__version__ == "0.0.1"
    assert core_config.name == "teatree.core"
    assert agents_config.name == "teatree.agents"
