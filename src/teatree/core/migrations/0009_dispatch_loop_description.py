from collections.abc import Callable

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

_PREFIX = "Runs the always-on global scanners every 5m: dispatches pending headless Tasks to phase sub-agents, "
_OLD = f"{_PREFIX}ingests incoming events, redelivers undelivered notifies, and posts deferred questions."
_NEW = f"{_PREFIX}ingests incoming events, redelivers undelivered notifies, and posts owner questions."


def _rewrite(before: str, after: str) -> Callable[[StateApps, BaseDatabaseSchemaEditor], None]:
    def run(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        loops = apps.get_model("core", "Loop").objects.using(schema_editor.connection.alias)
        loops.filter(name="dispatch", description=before).update(description=after)

    return run


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_deferred_question_evidence"),
    ]

    operations = [
        migrations.RunPython(_rewrite(_OLD, _NEW), _rewrite(_NEW, _OLD)),
    ]
