from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_OLD = (
    "Auto-closes high-confidence DEAD backlog issues (already-shipped / duplicate / obsolete) every 5m, "
    "only for t3-teatree owned repos; default-off behind auto_disposition_enabled, bounded per tick."
)
_NEW = (
    "Auto-closes high-confidence DEAD backlog issues (already-shipped / duplicate / obsolete) every 5m, "
    "only for t3-teatree owned repos; bounded per tick."
)


def _replace(apps: Apps, _schema_editor: BaseDatabaseSchemaEditor, *, old: str, new: str) -> None:
    loop = apps.get_model("core", "Loop")
    loop.objects.filter(name="issue_disposition", description=old).update(description=new)


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _replace(apps, schema_editor, old=_OLD, new=_NEW)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _replace(apps, schema_editor, old=_NEW, new=_OLD)


class Migration(migrations.Migration):
    dependencies = [("core", "0102_review_request_post_overlay")]

    operations = [migrations.RunPython(forwards, backwards)]
